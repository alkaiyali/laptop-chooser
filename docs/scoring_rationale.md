# Scoring Philosophy: Component Hierarchy & RAM Weighting

## The Hierarchy

For modern productivity, development, student, and office workflows in 2026, real-world utility follows a strict priority order:

$$\mathbf{CPU\ Single\text{-}Core} > \mathbf{CPU\ Multi\text{-}Core} > \mathbf{RAM} > \mathbf{GPU}$$

---

## Rationale & Key Assumptions

### 1. Gaming is a Niche, Not the Default (and "ML / AI" is No Excuse)
* Gaming-specific requirements (heavy dedicated GPUs) should not dictate the default scoring of a general laptop chooser.
* Gamers already have dedicated controls in the UI:
  * Dedicated GPU filter (`dedicated`, `RTX 50-series`, minimum VRAM).
  * The **Gaming Value** quick preset.
  * Direct sort by **Performance** or **Price**.
* For non-gamers, a dedicated GPU adds significant downsides: excess weight, fan noise, thermal throttling, idle power consumption (reduced battery life), and 1,500–3,000 SAR in BOM cost for silicon that sits idle 95% of the day.
* **The "ML & AI" Fallacy**:
  * Many students and engineers rationalize a gaming laptop for Machine Learning. But mobile GPUs (RTX 4050/4060) come with only **6GB–8GB of VRAM**, which hits instant CUDA Out-Of-Memory (OOM) on any modern neural network or non-trivial vision model.
  * Running an on-demand cloud GPU pod (RunPod, Lambda, Vast.ai, Colab Pro) gives access to real **NVIDIA H100 or A100 GPUs with 80GB VRAM** for $0.20 to $2.00 / hour (≈ 1–8 SAR/hr)—massively faster, cheaper, and with zero thermal/weight toll on your daily machine.
  * What you actually need on your desk is a lightweight laptop with **32GB+ of unified system RAM**, exceptional battery endurance, and a great keyboard/screen to SSH into remote cloud pods, run local dev environments, and multitask without memory exhaustion.

### 2. Throughput vs. Capacity/Threshold Resources
* **CPU (Single + Multi)**: Throughput resources. 
  * **Single-core** dictates everyday OS responsiveness, web app execution (V8 / JavaScript), app launch times, and single-threaded compilation bottlenecks.
  * **Multi-core** supports heavy multitasking, background jobs, parallel builds, and export workflows.
* **GPU**: Throughput resource, but in 2026, modern integrated graphics (Intel Arc 140V/140T, AMD Radeon 890M/780M, Apple M4) comfortably drive multi-monitor 4K setups, 8K video decode, hardware AV1 encoding, and lightweight 3D/CAD. More GPU beyond a solid iGPU provides **zero** benefit to 90% of laptop users.
* **RAM**: A **capacity and threshold resource**. 
  * When RAM runs out, the machine drops off a performance cliff due to SSD swap thrashing.
  * In 2026, modern workflows are memory-intensive:
    * Multiple Electron apps (VS Code, Slack, Teams, Notion) consume 1GB+ each.
    * Browser tabs with complex client-side applications.
    * Docker containers and local developer tools.
    * **Heavy Multitasking & Development**: Running local containers (Docker), background databases, language servers, heavy IDEs, and 50+ browser tabs quickly exhausts 16GB. 16GB is the entry baseline; 24GB–32GB is the true productivity sweet spot.
    * **The Soldered RAM Trap**: Most thin-and-lights solder their LPDDR5X memory. A machine purchased with insufficient RAM is permanently obsolete.

### 3. Expansion Potential & Obsolescence Risk (The Regional Retail Reality)
* **The Brutal Regional Pricing Reality**:
  * In Saudi Arabia (Darat, Amazon SA, Kanbkam) and Southeast Asia (PH / PC Express), standalone DDR5-5600 16GB SO-DIMMs are severely marked up by local distributors and importers, retailing at **860–1,035 SAR** (₱10,000–15,000) **per single stick**.
  * Crucially, laptops like the Lenovo IdeaPad Slim 5 14IMH10 ship with **two 8GB sticks** (2x8GB) occupying both SO-DIMM slots to run dual-channel out of the box. Upgrading to 32GB requires buying a **2x16GB kit, costing 1,700–2,050 SAR** (₱20,000–30,000), plus chassis disassembly risk and warranty friction.
* **The "Economic Trap" vs. Factory 32GB**:
  * Buying a 16GB machine for 3,439 SAR + spending 1,950 SAR on RAM = **~5,389 SAR total spent**.
  * In the unified catalog, **3,999 to 4,889 SAR** already buys a **factory 32GB laptop** (e.g. Honor MagicBook Pro 14 at 3,999 SAR, HP OmniBook 5 AI at 4,024 SAR, Asus Zenbook 14 at 4,889 SAR) with superior 3K 120Hz/OLED panels, faster H-series silicon, and full factory warranty covering the memory.
  * Therefore, planning to buy a 16GB laptop and upgrade it at regional retail prices is an **economic trap**.
* **The Calibrated Scoring Stance**:
  * Having an open SO-DIMM slot does **not** provide 32GB of memory. It offers modest salvage and repairability value (or a way to limp forward to 24GB via one stick if desperate).
  * Consequently, expansion headroom receives only a modest token bonus (**+0.5 to +1.0 pt max**):
    * SO-DIMM slot with headroom above fitted: **+0.5 pt** (repairability / serviceability)
    * Practical ceiling $\ge 32$GB: **+0.5 pt** (Total max: **+1.0 pt**)
* **The Calibrated Progression**:
  * **Soldered 16 GB**: **$8.6\text{ pts}$** (Clean baseline, zero upgrade path)
  * **Upgradable 16 GB ($\ge 32$GB)**: **$9.6\text{ pts}$** ($+1.0\text{ pt}$ ahead of soldered; recognizes physical serviceability without misleading the user that it's a 32GB substitute)
  * **Factory 24 GB**: **$12.6\text{ pts}$** ($+3.0\text{ pts}$ ahead of 16GB; "Goldilocks" tier capturing ~65% of the 32GB benefit for heavy multitasking and local container workflows at a lower price point)
  * **Pre-fitted 32 GB**: **$14.9\text{ pts}$** ($+5.3\text{ pts}$ ahead of upgradable 16GB; firmly directs buyers toward factory 32GB models where the economics actually make sense)
  * **Pre-fitted 64 GB+**: **$17.5\text{--}18.0\text{ pts}$** (Workstation powerhouse tier)

---

## Architectural Implementation

To faithfully embody the $\text{CPU Single} > \text{CPU Multi} > \text{RAM} > \text{GPU}$ hierarchy:

1. **CPU Weight**: **65%** of the silicon base:
   * Single-Core ($70\%$ of CPU): **$45.5\%$ effective weight** (Top priority).
   * Multi-Core ($30\%$ of CPU): **$19.5\%$ effective weight** (Second priority).
2. **RAM Weight**: Usable headroom above OS natural idle footprint ($7.0 \cdot \log_2(\text{usable} / 3.5)$):
   * $\le 8\text{ GB}$: Severe physical $-2.0$ choke penalty (plus $-3.0$ soldered trap) due to active pagefile thrashing.
   * $8\text{ GB}$ (3.5 GB usable): The zero-point capacity baseline ($0.0$ capacity points, $-2.0$ choke).
   * $16\text{ GB}$ (11.3 GB usable): Standard viable baseline ($+11.8$ points perf, $+7.1$ points overall; avoids choke).
   * $24\text{ GB}$ (19.5 GB usable): High-value "Goldilocks" sweet spot ($+17.3$ points perf, $+10.4$ points overall).
   * $32\text{ GB}$ (27.5 GB usable): Power productivity sweet spot ($+20.8$ points perf, $+12.5$ points overall).
   * $64\text{ GB}+$ (59.5 GB usable): Heavy engineering & workstation tier ($+28.7$ points perf, $+17.2$ points overall).
   * $128\text{ GB}$ (123 GB usable): Extreme workstation tier ($+35.9$ points perf, $+21.5$ points overall).
3. **RAM Expansion Headroom**: Capped at **+0.5 to +1.0 point** for machines with accessible SO-DIMM slots and a $\ge 32$GB ceiling, preventing the algorithm from overvaluing post-purchase retail upgrades.
4. **GPU Weight**: **15%** of the silicon base:
   * Properly rewards modern, capable iGPUs (Intel Arc, Radeon 890M/780M, Apple M4) over primitive display adapters (Intel UHD).
   * Prevents power-hungry dedicated gaming cards from artificially inflating general productivity rankings.

### OS Natural Idle Footprint & Usable Headroom Calibration

Evaluating raw installed RAM in isolation is misleading because operating system kernels, background daemons, security suites, and display server compositors consume physical memory before user applications can execute:

$$\text{Usable RAM} = \max(\text{Installed RAM} - \text{Base OS Idle Reserve} - \text{iGPU Frame Buffer},\, 0.5)$$

| Operating System | Base OS Reserve | iGPU Allocation | Total Idle Footprint | Usable on 8 GB | Usable on 16 GB | Architectural Rationale |
|---|---|---|---|---|---|---|
| **Windows 11 Pro** | 4.0 GB | +1.0 GB | **5.0 GB** | **3.0 GB** | **11.0 GB** | Hyper-V, Virtualization-Based Security (VBS), BitLocker, background telemetry |
| **Windows 11 Home** | 3.5 GB | +1.0 GB | **4.5 GB** | **3.5 GB** | **11.5 GB** | Consumer baseline; Windows Defender, background services, Edge pre-loader |
| **macOS (Apple Silicon)** | 3.5 GB | +1.0 GB | **4.5 GB** | **3.5 GB** | **11.5 GB** | Unified memory architecture; WindowServer compositor, Metal cache |
| **Windows 10 Pro / Home** | 3.0 – 3.5 GB | +1.0 GB | **4.0 – 4.5 GB** | **3.5 – 4.0 GB** | **11.5 – 12.0 GB** | Slightly leaner legacy desktop stack |
| **Linux (Ubuntu / Fedora)** | 1.5 – 2.0 GB | +1.0 GB | **2.5 – 3.0 GB** | **5.0 – 5.5 GB** | **13.0 – 13.5 GB** | Lean kernel architecture; minimal background services |
| **ChromeOS** | 2.0 GB | (Included) | **2.0 GB** | **6.0 GB** | **14.0 GB** | Browser-first operating system on lightweight Linux base |
| **FreeDOS / No OS** | 4.0 GB (Windows) | +1.0 GB | **5.0 GB** | **3.0 GB** | **11.0 GB** | Retrofit model: scored as the Windows OS the buyer installs within 48 hours |

#### The Non-Linear Usable Headroom Multiplier
While 16 GB is nominally $2.0\times$ of 8 GB on marketing spec sheets, in reality under Windows 11 with an integrated GPU:
* **8 GB Laptop**: $8.0 - 4.5 = \mathbf{3.5\text{ GB}}$ usable application memory.
* **16 GB Laptop**: $16.0 - 4.5 = \mathbf{11.5\text{ GB}}$ usable application memory.

The buyer gains **$11.5 \div 3.5 = \mathbf{3.28\times}$ more application headroom**! This non-linear multiplier explains why moving from 8 GB to 16 GB eliminates constant SSD pagefile thrashing, while 8 GB machines suffer an immediate $-2.0$ pt low RAM choke.

---

## How Performance and Value Scores are Measured

### 1. The Performance Score (`perf_score`) Model

Compute performance strictly measures silicon processing throughput, graphics power, and memory capacity & speed. Physical build materials (CNC aluminum vs. plastic), screen panels (OLED vs. IPS), storage drive sizes, and battery capacities are treated as independent quality and endurance attributes—they have their own card rows and filters in the chooser and do **not** artificially inflate compute performance.

```
perf_score = (0.65 × CPU_score) + (0.15 × GPU_score) + RAM_score
```

Where `RAM_score` accounts for usable memory capacity, memory bus generation (DDR5 vs DDR4), dedicated VRAM buffer bonuses/penalties, and memory choke penalties.

| Component | Source & Curve Formula | Effective Contribution |
|---|---|---|
| **CPU Score** (65%) | • Review-grade Notebookcheck Cinebench R23 (when $n \ge 3$) cross-calibrated into PassMark space ($r \approx 0.98$), or PassMark CPU Mark.<br>• Blend: $0.70 \times \text{Single-Thread} + 0.30 \times \text{Multi-Thread}$.<br>• Log-scale: Multi [1k–65k], Single [700–7k]. | **45.5%** Single-Core<br>**19.5%** Multi-Core |
| **GPU Score** (15%) | • PassMark G3D mobile laptop profile scaled logarithmically [500–42,000].<br>• Apple Silicon scaled by architecture & GPU core count (M4 8-core: 60.2 → M4 Max 40-core: 93.9).<br>• Workstation Ada (RTX 500–3000 Ada) mapped to measured ISV benchmarks. | **15.0%** Silicon Base |
| **RAM Score** | • Usable Headroom = Installed − OS idle reserve (Win: 4.0GB, macOS: 3.5GB, Linux: 1.5GB) − iGPU (+1.0GB).<br>• Capacity curve: $7.0 \times \log_2(\text{usable} / 3.5)$ (8GB = 0.0, 16GB = +11.8, 24GB = +17.3, 32GB = +20.8, 64GB = +28.7, 128GB = +35.9).<br>• Physical Low-RAM Choke: $-2.0$ pts if usable $\le 8$GB (penalizes disk thrashing & UI stutter).<br>• Soldered Trap: $-3.0$ pts surcharge on permanently soldered $\le 8$GB machines (`ram_trap`).<br>• RAM generation: $+0.75$ DDR5/LPDDR5(X), $-0.5$ DDR4.<br>• Upgrade headroom: $+0.5$ open slot, $+0.5$ ceiling $\ge 32$GB (max $+1.0$).<br>• Dedicated VRAM: $+0.75$ for $\ge 12$GB, $-0.5$ for $\le 6$GB. | **−5.0 to +36.0+ pts**<br>(8GB Soldered = −4.25, 16GB DDR5 = +12.55, 32GB DDR5 = +21.55) |
| **Hardware & Quality** | • Standardized 0–100 Quality Index combining:<br>  1. Display Quality (35 pts max): `(display_score / 10) * 35`<br>  2. Chassis & Build (30 pts max): `clamp((build_score - 4.5) / 5.0, 0, 1) * 30` (anchored at 4.5 plastic floor to penalize cheap chassis)<br>  3. Battery & Mobility (20 pts max): `clamp((Wh - 35)/65, 0, 1) * 20` (minus dGPU idle tax)<br>  4. Storage & Extras (15 pts max): SSD capacity tier (5–15 pts) + touch/16"+ extras.<br>• **40% Weight in Overall Value**: Balanced alongside 60% compute throughput. | **40.0% of Overall**<br>(0.0 pts to perf_score) |

---

### 2. The Value Score (`value_score`) Model

```
overall_score = (0.60 × perf_score) + (0.40 × quality_score)
Value Score = (overall_score ÷ (price_SAR + 1,500)) × 2,000
```

1. **60/40 Compute-First Balanced Dual 0–100 Indices (60% Compute / 40% Hardware & Quality)**:
   * When buying a laptop, computing throughput is the primary engine (60% weight, 1.5× the weight of physical build).
   * Combining `perf_score` (60%) and `quality_score` (40%) into `overall_score` ensures that premium OLED displays, all-metal unibodies, and 75Wh+ batteries provide a decisive +12 to +15 point overall advantage over dim, plastic chassis machines, while preventing physical luxury from eclipsing modern silicon capability.
2. **The 1,500 SAR Fixed Ownership Floor**:
   * A raw ratio $\text{overall} / \text{price}$ forms a hyperbola that shoots to infinity as price approaches zero.
   * Every functioning laptop carries an irreducible baseline manufacturing and delivery cost (chassis tooling, battery cells, display glass, motherboard PCB, power supply, keyboard, packaging, safety certifications, and warranty).
   * Adding 1,500 SAR to the denominator models this fixed overhead, eliminating the zero-denominator singularity and ensuring that value reflects return per total capital deployed.
3. **The 2,000 Scale Normalizer**:
   * Maps the output into an intuitive 0–100 consumer scale: **20–28** is fair market territory, **28–35** is a great deal, and **35+** marks exceptional clearance outliers.
4. **Cross-Currency Parity**:
   * Multi-store listings (e.g. DataBlitz PHP) are converted to SAR using live official bank exchange rates (1 PHP ≈ 0.061 SAR). The engine automatically selects the lowest in-stock price.

---

### 3. Algorithmic Deal Outlier Engine (🔥 Deal N×)

Instead of relying on inflated retailer "strike-through" discounts, the system runs an algorithmic peer comparison:
1. Isolates the **25 closest matching spec peers** in the catalog (matched on CPU score tier, RAM capacity, GPU class, and storage).
2. Computes the **median value score** of that 25-peer group.
3. If a machine's value score is **$\ge 1.5\times$ the median of its peers**, it receives the **🔥 Deal N×** badge.

---

### 4. Step-by-Step Calculation Walkthrough

**Acer Swift Go 14 SFG14-74-515N (SAR 3,628)**:
* **Compute Performance Steps (50% weight)**:
  * **CPU Score**: Intel Core Ultra 5 225H
    * Single-thread: 4,255 mark → 78.4 · Multi-thread: 28,098 mark → 79.9
    * Blended: $0.7 \times 78.4 + 0.3 \times 79.9 = 78.9$ → Weighted: $0.65 \times 78.9 = \mathbf{51.29\text{ pts}}$
  * **GPU Score**: Intel Arc 140T (iGPU)
    * PassMark G3D: 6,601 mark → GPU score = 58.2 → Weighted: $0.15 \times 58.2 = \mathbf{8.73\text{ pts}}$
  * **RAM Capacity & Generation**: 16 GB LPDDR5X
    * Usable headroom: 11.0 GB (after Windows 11 reserve and iGPU carve-out)
    * Capacity curve: $7.0 \times \log_2(11.0 / 3.5) = \mathbf{11.6\text{ pts}}$ (no choke, usable > 8 GB)
    * LPDDR5X generation bonus: $+0.75\text{ pts}$
    * Total RAM score: $11.6 + 0.75 = \mathbf{12.35\text{ pts}}$
  * **Total Performance Score (`perf_score`)**:
    $$\text{perf\_score} = 51.29 + 8.73 + 12.35 = \mathbf{72.4 / 100}$$
* **Hardware & Quality Steps (40% weight)**:
  * Display: 14" 2.8K OLED panel (10/10) → $(10.0 / 10) \times 35.0 = \mathbf{35.0\text{ pts}}$
  * Chassis: All-metal build (8.8/10) → $(8.8 / 10) \times 30.0 = \mathbf{26.4\text{ pts}}$
  * Battery: 75 Wh (Spec-matched) → $((75 - 35) / 65) \times 20.0 = \mathbf{12.3\text{ pts}}$
  * Storage & Extras: 512 GB NVMe SSD → $\mathbf{7.4\text{ pts}}$
  * **Total Quality Score (`quality_score`)**:
    $$\text{quality\_score} = 35.0 + 26.4 + 12.3 + 7.4 = \mathbf{81.1 / 100}$$
* **Total Overall Score (`overall_score`)**:
  $$\text{overall\_score} = 0.60 \times 72.4 + 0.40 \times 81.1 = 43.44 + 32.44 = \mathbf{75.9\text{ pts}}$$
* **Final Value Score**:
  $$\text{Value} = \left(\frac{75.9}{3,628 + 1,500}\right) \times 2,000 = \left(\frac{75.9}{5,128}\right) \times 2,000 = \mathbf{29.60}$$

---

## 5. Deconstructing the "Near-Perfect Law": The Pareto Frontier & The Knee Point

### 5.1 Why the Scatter Plot Looks Like an Immutable Economic Law

When plotting laptops by **Price vs. Value** (or **Price vs. Performance**), the upper boundary of the market traces an extraordinarily smooth, textbook hyperbolic decay curve. What appears at first glance to be a mysterious immutable law of economics is actually the direct confluence of **three compounding mathematical and physical factors**:

1. **Spurious Correlation & Mathematical Convexity ($1/X$ Hyperbolic Decay)**:
   * The Y-axis is Value Score:
     $$\text{Value} = \left(\frac{\text{Overall}}{\text{Price} + 1,500}\right) \times 2,000$$
   * Plotting any positive, bounded function $Y = \frac{f(X)}{X + c}$ directly against $X$ mathematically forces an inverse $1/X$ decay curve.
   * **Empirical Proof**: If laptop performance were replaced with completely uncorrelated, uniform random noise $f(X) \sim U(40, 80)$, plotting $U / X$ versus $X$ still generates a Pearson correlation of $r \approx -0.85$ and a smooth falling hyperbolic curve. Plotting a ratio against its own denominator mathematically creates an apparent inverse power law.

2. **Silicon Supply Chain Economics & Physical Diminishing Returns**:
   * Computer hardware components face severe physical and economic ceilings governed by semiconductor wafer yields, die size scaling, and thermal dissipation limits:
     * **At the Budget/Mid-Range (SAR 1,500 – 4,000)**: Every 500 SAR step moves the buyer from 4-core low-IPC chips to modern 8/12-core silicon, from 8 GB to 16/32 GB of system RAM, and from washed-out 45% NTSC panels to 2.8K 120Hz OLEDs. Marginal gains are massive: **+15.6 overall points per SAR 1,000 spent**.
     * **At the Flagship Tier (SAR 7,000 – 24,000)**: Pushing clock frequencies and core counts further incurs super-linear cost growth ($O(e^k)$) for silicon binning, vapor chambers, and unibody machining, while real-world user utility flattens logarithmically ($O(\log k)$). A SAR 24,000 workstation delivers only 2.5× the raw compute of a SAR 3,600 machine, but costs over 6.8× more. Bang-for-the-buck collapses rapidly on the left and flattens into a long horizontal tail on the right.

3. **Pareto Optimal Upper-Envelope Tracing**:
   * The Pareto Frontier isolates the non-dominated set of machines: every laptop where no cheaper alternative beats it in score.
   * By definition, this envelope systematically filters out all inefficient retail noise (opportunistic dealer markups, outdated SKUs, uncompetitive specs).
   * Filtering any two-dimensional scatter cloud for its running upper bound produces an inherently monotonic curve $y^*(x)$ with a strictly negative second derivative ($y''(x) < 0$), transforming noisy real-world retail pricing into a pristine, textbook boundary.

---

### 5.2 Empirical Saudi Market Correlation Matrix ($N = 1,294$)

Full statistical correlation across all priced, in-stock laptops in our regional database confirms this dual reality:

| Variable Pair | Pearson ($r$) | Spearman Rank ($\rho$) | Log-Transformed Price ($r$) | Explained Variance ($R^2$) | Economic Interpretation |
| :--- | :---: | :---: | :---: | :---: | :--- |
| **Price vs. Performance** | **+0.7901** | +0.8440 | **+0.8661** | 75.0% | Compute power scales strongly with price, following a logarithmic rather than linear trajectory. |
| **Price vs. Quality** | **+0.7125** | +0.7985 | +0.7710 | 59.4% | Displays, metal unibodies, and battery sizes scale with price, but saturate near SAR 6,500. |
| **Price vs. Overall Score** | **+0.8250** | **+0.9067** | **+0.8875** | 78.8% | Combined utility has an exceptional rank correlation ($\rho > 0.90$): more money reliably buys a better total machine. |
| **Price vs. Value Score** | **-0.8800** | **-0.9293** | **-0.9360** | **87.6%** | Value decays almost perfectly with log-price. 87.6% of all value variance is explained by price tier alone. |

---

### 5.3 The Pareto Knee: Maximum Marginal Efficiency

In multi-objective optimization, the **Pareto Knee (or Elbow)** is the point of maximum geometric curvature along the trade-off frontier. It defines the point of diminishing returns—the exact boundary where spending more money ceases to yield proportional improvements:

* **Below SAR 4,000 (The Steep Climb)**: Moving from SAR 1,599 (Acer Aspire Lite, 41.5 overall) to SAR 3,628 (Acer Swift Go 14, 75.9 overall) delivers **+16.9 overall points per SAR 1,000 spent**.
* **The Knee Zone (SAR 3,600 – SAR 5,500)**: This is the global sweet spot. Laptops in this cluster score **75 to 82 overall points** (capturing 75%–82% of the absolute best machines in the world), yet cost only **15% to 22% of flagship workstation prices**.
* **Above SAR 5,500 (Diminishing Returns)**: Moving from SAR 5,500 to SAR 24,679 (MacBook Pro M4 Max, 96.6 overall) yields only **+0.86 to +0.99 points per SAR 1,000 spent**—an **18-fold drop in marginal efficiency**.

---

### 5.4 Standout Knee Configurations in the Regional Market

| Machine | Price | Overall | Perf | Value | Why It Is a Pareto Knee |
| :--- | :---: | :---: | :---: | :---: | :--- |
| **Acer Swift Go 14 (SFG14-74)** | **SAR 3,628** | **75.9** | 72.4 | **29.60** | **#1 Overall Knee**: Core Ultra 5 225H, 16 GB LPDDR5X, 14" 2.8K OLED (10/10), all-metal build. Unmatched quality-per-riyal. |
| **HP 15-fd1037nx** | **SAR 2,949** | **61.1** | **73.9** | **27.47** | **Budget Compute Knee**: Core i7-1355U / high single-core compute under 3,000 SAR for office/code workflows. |
| **Acer Swift 14 AI (SF14-51)** | **SAR 4,146** | **77.9** | 75.1 | **27.59** | **Modern AI Knee**: Core Ultra 7 with dedicated NPU, 100% sRGB 120Hz display, and aluminum build. |
| **HP OmniBook X Flip 14** | **SAR 4,878** | **79.5** | 78.6 | **24.93** | **2-in-1 Versatility Knee**: Touch convertible OLED with AMD/Snapdragon silicon and premium 360° hinge. |
| **Asus Zenbook 14 (UX3405CA)** | **SAR 5,353** | **78.9** | 74.0 | **23.03** | **Premium Ultrabook Knee**: 3K 120Hz OLED, 75 Wh battery, MIL-STD metal unibody; flagship feel without five-figure markup. |
| **Lenovo LOQ 15IRX10** | **SAR 5,399** | **67.9** | **86.8** | **19.68** | **Gaming / GPU Compute Knee**: Core i7-13650HX with full-power RTX 4060 dGPU; breaks the 85-point compute barrier at lowest market entry. |
| **Asus Zenbook S / Zenbook 14** | **SAR 6,199–6,499** | **80.0–81.0** | 76.2–76.4 | **20.00–21.00** | **Flagship 32 GB Knee**: Ceraluminum unibody, factory 32 GB memory, 3K 120Hz OLED. The final inflection point before diminishing returns turn severe. |

---

## 6. Beyond Aggregate Scores: Discrete Specs & Buyer Dealbreakers

An aggregate "Quality Score" or "Build Rating" is a valuable macro metric, but retail listings often use marketing buzzwords to obscure daily dealbreakers. For example, budget lines like the **HP 15-fd series** advertise "Full HD (1920×1080)" and "USB Type-C", while physical teardowns reveal:
1. **The Display**: Often a 250-nit, 45% NTSC non-IPS (TN) panel with poor viewing angles and washed-out colors.
2. **The Ports**: The USB-C port is 5Gbps data-only and **cannot charge the laptop**, forcing users to carry a proprietary 4.5mm blue-pin barrel charger.
3. **The Battery**: Sized at only 41 Wh, delivering 3 to 4.5 hours of real-world use on a 15.6" machine.
4. **The Keyboard**: Non-backlit on entry-level configurations.
5. **The Build**: 100% polycarbonate plastic with no metal parts (corrected from earlier 6.5 rating to 4.8 plastic).

### 6.1 Surfaced Dealbreaker Warnings (`dealbreakers`)
To protect buyers from hidden compromises, the data pipeline and web viewer compute explicit warning pills:
* `⚠️ Proprietary Barrel Charger (No USB-C PD)`: Machine requires an AC barrel brick and cannot charge over USB-C.
* `⚠️ Non-IPS / Budget Screen (250 nits, 45% NTSC)`: Identifies low-contrast TN/budget LCD panels.
* `⚠️ Small Battery (≤42 Wh)`: Flags short-battery laptops on 14"+ form factors.
* `⚠️ No Keyboard Backlight`: Discloses unlit keyboards on budget SKUs.
* `⚠️ Older DDR4 RAM on Modern CPU`: Flags older memory generation throttling integrated graphics bandwidth on 13th/14th Gen or Core Ultra processors.
* `⚠️ 8GB Soldered RAM (Cannot Upgrade)`: Flags permanent memory traps.

### 6.2 Verified Hardware Advantages (`advantages`)
Conversely, legitimate engineering strengths are surfaced as pro badges:
* `✔️ Backlit Keyboard`: Confirmed key illumination for low-light productivity; awards a direct **+0.4 build score bonus** to chassis ergonomics.
* `✔️ 2x Upgradable SO-DIMM Slots (Up to 32/64GB)`: Full RAM modularity (as found on the HP 15-fd).
* `✔️ USB-C Power Delivery`: Universal one-charger travel convenience.
* `✔️ OLED / 100% sRGB Display`: True color accuracy and high contrast.
* `✔️ 1080p FHD Webcam`: Clear video conferencing with privacy shutters.
* `✔️ Large Battery (≥70 Wh)`: Genuine all-day untethered endurance.
* `✔️ Premium All-Metal Chassis`: Rigid unibody aluminium/magnesium construction.

### 6.3 Keyboard Backlight Build Bonus Integration
Illuminated keyboards represent a tangible ergonomic and build premium. Machines with verified keyboard backlights (`keyboard_backlit: True`):
1. **Direct Build Score Boost**: Receive a direct **+0.4 point bonus** on `build_score` (capped at 10.0), shifting edge cases into higher tiers (e.g. 5.6 -> 6.0 Mainstream or 7.2 -> 7.6 Solid) and re-evaluating `build_label`.
2. **Material Annotation**: `build_material` description appends `" · backlit keyboard"`.
3. **Downstream Value Lift**: Through `compute_quality_score()` and `apply_quality_points()`, this +0.4 build lift awards $+2.4$ quality points (out of 100) and $+0.96$ points to `overall_score` ($0.40 \times 2.4$), improving the machine's competitive Pareto ranking.
4. **Verified Advantage Badge**: Displays the `✔️ Backlit Keyboard` badge on laptop cards and in the comparison matrix.

---

## 7. Mobile Workstation Benchmark (RAM + CPU + GPU Only)

While the default `perf_score` and `overall_score` are calibrated for everyday professional and student workflows (where battery life, display panel, chassis materials, and light single-core responsiveness dominate), heavy computational tasks require a **Pure Compute Workstation Benchmark**.

### 7.1 The Three Foundation Pillars: RAM, CPU, and GPU
In workstation workflows (3D modeling & rendering in Blender/Cinema4D, CAD assemblies in SolidWorks/Autodesk Inventor, local machine learning & LLM fine-tuning, large C++/Rust/Go compilation, and 8K video rendering in DaVinci Resolve), physical chassis materials, battery life, and screen tech have **zero weight**. Only **three resources dictate whether the machine can execute the task without crashing or throttling**:

$$\mathbf{WS\ Score} = 0.35 \cdot \text{CPU}_{\text{multi}} + 0.35 \cdot \text{GPU}_{\text{compute}} + 0.30 \cdot \text{RAM}_{\text{capacity}}$$

1. **CPU (35%) — Multi-Thread Throughput Priority**:
   * Re-weighted for multi-core throughput: **$65\%$ multi-thread / $35\%$ single-thread** PassMark score.
   * Compiling 100,000 lines of code, physics simulations, and CPU offline rendering scale with total thread throughput rather than idle clock speeds.
2. **GPU (35%) — Compute & Viewport Throughput**:
   * Evaluates PassMark G3D compute power and CUDA acceleration.
   * Dedicated GPUs (NVIDIA RTX 4050/4060/4070/4080/4090/Ada and Apple M3/M4 Max GPUs) deliver $5\times\text{--}15\times$ the throughput of integrated chips for GPU rendering, tensor cores, and CAD viewports.
3. **RAM Capacity (30%) — Threshold Memory Curve**:
   * Workstation tasks require large memory pools; exceeding physical RAM triggers SSD swap pagefile thrashing, dropping performance by $99\%$.
   * Logarithmically scaled:
     * $\le 8\text{ GB}$: $10\text{--}25\text{ pts}$ (severe workstation bottleneck)
     * $16\text{ GB}$: $50\text{ pts}$ (entry workstation baseline)
     * $24\text{ GB}$: $63\text{ pts}$
     * $32\text{ GB}$: $75\text{ pts}$ (standard professional CAD / 4K editing baseline)
     * $64\text{ GB}$: $90\text{ pts}$ (heavy multi-dataset & local ML sweet spot)
     * $128\text{ GB}+$: $100\text{ pts}$ (extreme workstation desktop replacement)







