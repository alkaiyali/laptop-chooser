"""Heuristic display and build/material ratings for normalized laptop records.

Neither store publishes panel colour gamut or chassis materials, so both ratings are
estimates built the way a buyer would reason:

* **Display** — panel technology (OLED / Mini-LED / IPS / TN), resolution and pixel
  density, refresh rate, plus brightness / gamut / HDR when the title or specs happen
  to mention them.  Budget consumer LCDs in this market are typically 250-300 nit,
  45% NTSC (~62% sRGB) panels, so non-OLED machines from mainstream/budget lines take
  a "typical budget panel" penalty; business lines (ProBook, ThinkPad, Latitude) are
  usually a little brighter, so they only take a small one.

* **Build/material** — driven by the product line in the name (Zenbook = aluminium,
  Vivobook = plastic, ProBook = aluminium lid over a plastic base, ThinkPad T/X =
  magnesium, MacBook = unibody aluminium, ...), adjusted by material words found in
  the title or specs (carbon, magnesium, aluminium, metal, MIL-STD).

  Every line in FAMILIES is pinned against vendor specs or review teardowns
  (HP 15-fd/fc aluminium lid, X1605VA 24 GB chassis, Dell 16 Plus alu
  cover+base, Aero X16 aluminium, MegaBook all-metal, GT Book alu lid+base,
  ...). Lines with no professional reviews (INFINIARC) say "unverified" in
  the note. When the title carries no product line at all (stripped
  marketplace feeds), BRAND_DEFAULTS scores the vendor's segment instead of
  leaving the record "unclassified" — with a dedicated-GPU floor so a bare
  "Gaming Laptop" with an RTX never ranks as budget plastic.

Scores are 1-10, aligned with the 6/10 (Vivobook plastic), 7/10 (ProBook alu lid),
8/10 (IdeaPad Slim 5 alu) and 9/10 (Zenbook metal) figures used in the shortlist doc.
"""

from __future__ import annotations

import re

# --------------------------------------------------------------------------- helpers

def _blob(rec: dict) -> str:
    """Searchable lowercase text: title + short description + spec values."""
    parts = [rec.get("name") or "", rec.get("model") or "", rec.get("short_description") or ""]
    specs = rec.get("specs") or {}
    parts.extend(f"{k} {v}" for k, v in specs.items())
    return " ".join(parts).lower()


def _clamp(x: float) -> float:
    return round(max(1.0, min(10.0, x)), 1)


# --------------------------------------------------------------------- build rating

# (regex on the product name, score, material note, build class)
# most specific patterns first — the first match wins
FAMILIES: list[tuple[str, float, str, str]] = [
    # Apple
    (r"macbook pro", 10.0, "unibody aluminium (MacBook Pro)", "premium"),
    (r"macbook air", 9.5, "unibody aluminium (MacBook Air)", "premium"),
    (r"macbook", 9.5, "unibody aluminium (MacBook)", "premium"),
    # Lenovo
    (r"thinkpad x1|thinkpad x9", 9.3, "carbon fibre / magnesium (ThinkPad X1 class)", "premium"),
    (r"thinkpad (t|p|x)\d?", 9.0, "magnesium-aluminium (ThinkPad T/P/X class)", "premium"),
    (r"thinkpad (e|l)\d?", 7.0, "aluminium lid, plastic base (ThinkPad E/L class)", "business"),
    (r"\be1[46]\b", 7.0, "aluminium lid, plastic base (ThinkPad E class)", "business"),
    (r"lenovo v1[457]\b|\bv1[457] (g\d|irl)\b", 4.5, "basic plastic (Lenovo V class)", "budget"),
    (r"thinkpad", 8.0, "business-grade plastic/magnesium (ThinkPad)", "business"),
    (r"thinkbook", 6.5, "aluminium lid, plastic base (ThinkBook class)", "business"),
    (r"yoga book", 9.5, "aluminium, dual screen (Yoga Book)", "premium"),
    (r"yoga slim", 8.5, "aluminium body (Yoga Slim class)", "premium"),
    (r"yoga", 8.5, "aluminium body (Yoga class)", "premium"),
    (r"ideapad slim 5|ideapad pro", 8.0, "aluminium top and bottom (IdeaPad Slim 5/Pro class)", "mainstream"),
    (r"ideapad slim 3|ideapad slim 1|ideapad 1|ideapad 3", 5.6, "plastic body (IdeaPad Slim 3 class)", "budget"),
    (r"ideapad (flex|gaming|5)", 6.0, "plastic body (IdeaPad Flex/Gaming class)", "mainstream"),
    (r"ideapad", 5.6, "plastic body (IdeaPad class)", "budget"),
    (r"lenovo v\d|lenovo (1|3)\d\d", 4.5, "basic plastic (Lenovo V class)", "budget"),
    (r"legion", 7.5, "aluminium lid, plastic base (Legion class)", "gaming"),
    (r"\bloq\b", 5.5, "plastic body (LOQ class)", "gaming"),
    # HP
    (r"dragonfly", 9.0, "magnesium (Dragonfly class)", "premium"),
    (r"pavilion aero|omnibook \d+ aero|\baero 13\b", 8.5, "magnesium (HP Aero class)", "premium"),
    (r"spectre", 9.5, "CNC aluminium (Spectre class)", "premium"),
    (r"elitebook", 9.0, "aluminium / magnesium (EliteBook class)", "premium"),
    (r"zbook", 8.5, "aluminium / magnesium (ZBook class)", "business"),
    (r"probook", 7.0, "aluminium lid over a plastic base (ProBook class)", "business"),
    (r"omnibook ?(x|ultra)", 8.5, "aluminium (OmniBook X/Ultra class)", "premium"),
    (r"omnibook ?5", 7.0, "aluminium/recycled metal lid + keyboard frame, plastic base (OmniBook 5 class)", "mainstream"),
    (r"omnibook", 6.5, "mixed plastic/metal (OmniBook class)", "mainstream"),
    (r"envy", 8.5, "aluminium body (Envy class)", "premium"),
    (r"pavilion", 6.0, "plastic body (Pavilion class)", "mainstream"),
    (r"victus", 5.8, "plastic body (Victus class)", "gaming"),
    (r"omen", 8.0, "aluminium lid, plastic base (OMEN class)", "gaming"),
    (r"15[st]?[- ]f[cd]", 4.8, "polycarbonate plastic body (HP 15-f series)", "budget"),
    (r"\b(250|255)[a-z]? ?g\d{1,2}\b|\bhp notebook\b", 4.8, "plastic body (HP 250 class)", "budget"),
    (r"hp (laptop|15|17|240|250|255)", 4.8, "plastic body (HP consumer class)", "budget"),
    # Dell / Alienware
    (r"xps", 9.5, "CNC aluminium + carbon (XPS class)", "premium"),
    (r"alienware", 8.2, "aluminium + magnesium (Alienware class)", "gaming"),
    (r"latitude[- ]?(9|7)", 9.0, "magnesium / carbon (Latitude 7000/9000 class)", "premium"),
    (r"latitude[- ]?5", 7.5, "aluminium lid, plastic base (Latitude 5000 class)", "business"),
    (r"latitude[- ]?3", 6.5, "plastic body (Latitude 3000 class)", "business"),
    (r"precision", 8.5, "aluminium / magnesium (Precision class)", "business"),
    (r"pro 1[456] essential|pv1525\d", 6.5, "plastic body (Dell Pro Essential class)", "business"),
    (r"dell pro max", 8.0, "aluminium (Dell Pro Max)", "premium"),
    (r"dell pro\b|\bpro 14\b|\bpro 16\b", 7.5, "aluminium lid, plastic base (Dell Pro class)", "business"),
    (r"inspiron 1[46] plus", 7.5, "aluminium cover and base (Inspiron Plus class)", "mainstream"),
    (r"\b16 plus\b", 7.5, "aluminium cover and base (Dell Plus class)", "mainstream"),
    (r"inspiron", 5.5, "plastic body (Inspiron class)", "budget"),
    (r"vostro", 6.0, "plastic body (Vostro class)", "mainstream"),
    (r"dell g ?1[56]\b|\bg1[56] \d{4}\b", 6.5, "plastic body (Dell G class)", "gaming"),
    (r"dell", 5.8, "plastic body (Dell consumer class)", "mainstream"),
    # ASUS
    (r"zenbook", 8.5, "all-aluminium (Zenbook class)", "premium"),
    (r"expertbook", 8.0, "aluminium / magnesium (ExpertBook class)", "business"),
    (r"proart", 8.5, "aluminium body (ProArt class)", "premium"),
    (r"vivobook s ?1[456]", 7.0, "aluminium lid, plastic base, MIL-STD (Vivobook S class)", "mainstream"),
    (r"\bx1504", 6.0, "plastic body (Vivobook Go class)", "mainstream"),
    (r"vivobook", 6.0, "plastic body (Vivobook class)", "mainstream"),
    (r"rog zephyrus|rog flow", 8.5, "aluminium + magnesium (ROG Zephyrus/Flow class)", "gaming"),
    (r"\brog\b", 7.5, "aluminium lid, plastic base (ROG class)", "gaming"),
    (r"\btuf\b", 6.2, "plastic body, MIL-STD tested (TUF class)", "gaming"),
    (r"asus", 5.8, "plastic body (ASUS consumer class)", "mainstream"),
    # Acer
    (r"swift air", 6.0, "metal but very thin — flexes (Swift Air class)", "mainstream"),
    (r"swift (14 ai|go|x|7|3)|\bswift\b", 8.5, "aluminium body (Swift class)", "premium"),
    (r"\ba31[56]\b", 5.5, "plastic body (Aspire class)", "budget"),
    (r"aspire", 5.5, "plastic body (Aspire class)", "budget"),
    (r"nitro", 6.0, "plastic body (Nitro class)", "gaming"),
    (r"predator|helios", 7.5, "aluminium lid, plastic base (Predator class)", "gaming"),
    (r"travelmate", 6.5, "plastic body (TravelMate class)", "business"),
    (r"acer", 5.5, "plastic body (Acer consumer class)", "mainstream"),
    # Microsoft
    (r"surface laptop", 9.2, "magnesium-aluminium (Surface Laptop class)", "premium"),
    (r"surface pro", 8.8, "magnesium with kickstand (Surface Pro class)", "premium"),
    (r"surface go", 7.0, "magnesium (Surface Go class)", "mainstream"),
    (r"surface", 8.5, "magnesium-aluminium (Surface)", "premium"),
    # Samsung
    (r"galaxy book.*(pro|ultra|edge)", 8.5, "aluminium body (Galaxy Book Pro/Ultra class)", "premium"),
    (r"galaxy book", 8.0, "aluminium body (Galaxy Book class)", "mainstream"),
    # Huawei / Honor / Xiaomi
    (r"matebook x pro", 9.2, "unibody aluminium (MateBook X Pro)", "premium"),
    (r"matebook (d|e|b)|matebook (14|15|16)", 6.5, "aluminium lid, plastic base (MateBook D class)", "mainstream"),
    (r"matebook", 7.5, "aluminium body (MateBook class)", "premium"),
    (r"magicbook (x pro|pro|art)", 8.0, "aluminium body (MagicBook Pro class)", "mainstream"),
    (r"magicbook", 6.4, "aluminium lid, plastic base (MagicBook class)", "mainstream"),
    (r"redmibook", 6.5, "aluminium lid, plastic base (RedmiBook class)", "mainstream"),
    (r"xiaomi book", 8.0, "aluminium body (Xiaomi Book class)", "premium"),
    # MSI / Razer / Framework
    (r"commercial", 7.5, "aluminium lid and base (MSI Commercial class)", "business"),
    (r"creator( pro)? m16", 6.0, "plastic body (Creator M16 class)", "gaming"),
    (r"summit", 9.0, "aluminium body (Summit class)", "premium"),
    (r"prestige|stealth", 7.5, "aluminium lid, plastic base (Prestige/Stealth class)", "premium"),
    (r"raider|titan", 8.3, "aluminium + magnesium (Raider/Titan class)", "gaming"),
    (r"vector", 7.0, "plastic body with metal lid (Vector class)", "gaming"),
    (r"modern|venture|katana|cyborg|thin|crosshair", 6.0, "plastic body (MSI Modern class, recent generations)", "gaming"),
    (r"razer blade", 9.0, "CNC aluminium (Blade class)", "premium"),
    (r"razer", 8.5, "CNC aluminium (Razer)", "premium"),
    (r"framework", 7.5, "recycled aluminium, user-repairable (Framework)", "mainstream"),
    (r"\bmsi\b", 6.0, "plastic body (MSI consumer class)", "gaming"),
    # Gigabyte
    (r"gigabyte.*aero|aero x16", 8.0, "aluminium body (Aero class)", "premium"),
    (r"gaming a16|\beagle\b", 6.0, "plastic body (Gigabyte budget gaming class)", "gaming"),
    (r"aorus", 8.5, "aluminium body (Aorus class)", "premium"),
    (r"gigabyte", 5.8, "plastic body (Gigabyte class)", "mainstream"),
    # Transsion (Tecno / Infinix) — vendor-advertised metal builds, review-backed
    (r"megabook", 7.5, "all-metal body (MegaBook class)", "mainstream"),
    (r"gt[ -]?book", 7.5, "aluminium lid and base (GT Book class)", "gaming"),
    (r"inbook", 7.0, "aluminium-magnesium (InBook class)", "mainstream"),
    # local / niche brands with no professional reviews: score the segment
    # honestly and say so in the note instead of claiming specifics
    (r"infiniarc", 5.5, "plastic body (INFINIARC gaming class, unverified)", "gaming"),
    (r"evol|colorful p15", 6.5, "metal lid, plastic base (Colorful gaming class)", "gaming"),
    (r"colorful", 5.5, "plastic body (Colorful class)", "mainstream"),
]

# material words found in titles/specs — only counted when the family note doesn't
# already imply that material
_MATERIAL_WORDS = [
    (re.compile(r"carbon ?fibre|carbon ?fiber|\bcarbon\b"), 0.7, "carbon"),
    (re.compile(r"magnesium"), 0.6, "magnesium"),
    (re.compile(r"aluminium|aluminum|unibody"), 0.5, "aluminium"),
    (re.compile(r"\bmetal\b"), 0.4, "metal"),
    (re.compile(r"mil[- ]?std"), 0.25, "mil-std"),
]

BUILD_LABELS = [(9.5, "Flagship"), (8.5, "Premium"), (7.5, "Solid"), (6.5, "Good"),
                (5.5, "Mainstream"), (4.5, "Basic")]


# last resort when no product line matches (stripped marketplace titles):
# the vendor's segment default, so a bare "Gaming Laptop" doesn't outrank
# real budget lines and a bare Dell doesn't hide as "unclassified"
BRAND_DEFAULTS = {
    "apple": (9.2, "unibody aluminium (Apple default)", "premium"),
    "microsoft": (8.5, "magnesium-aluminium (Surface default)", "premium"),
    "samsung": (8.0, "aluminium body (Galaxy Book default)", "premium"),
    "huawei": (7.5, "aluminium body (MateBook default)", "premium"),
    "razer": (8.5, "CNC aluminium (Razer default)", "premium"),
    "honor": (6.8, "aluminium lid, plastic base (Honor default)", "mainstream"),
    "xiaomi": (7.0, "aluminium body (Xiaomi default)", "mainstream"),
    "tecno": (7.5, "all-metal body (MegaBook default)", "mainstream"),
    "infinix": (7.2, "aluminium lid, plastic base (Infinix default)", "mainstream"),
    "msi": (6.0, "plastic body (MSI default)", "gaming"),
    "dell": (5.8, "plastic body (Dell default)", "mainstream"),
    "asus": (5.8, "plastic body (ASUS default)", "mainstream"),
    "lenovo": (5.8, "plastic body (Lenovo default)", "mainstream"),
    "acer": (5.5, "plastic body (Acer default)", "mainstream"),
    "hp": (4.8, "plastic body (HP default)", "budget"),
    "lg": (7.5, "magnesium (LG gram default)", "premium"),
    "gigabyte": (5.8, "plastic body (Gigabyte default)", "mainstream"),
    "colorful": (5.5, "plastic body (Colorful default)", "mainstream"),
    "infiniarc": (5.5, "plastic body (INFINIARC default)", "gaming"),
}


def rate_build(rec: dict) -> dict:
    """Return {'score', 'label', 'material', 'class'} for a record."""
    name = ((rec.get("name") or "") + " " + (rec.get("model") or "")).lower()
    blob = _blob(rec)

    score, material, klass = 5.5, "plastic body (unclassified)", "mainstream"
    matched = False
    for pattern, value, note, group in FAMILIES:
        if re.search(pattern, name):
            score, material, klass = value, note, group
            matched = True
            break
    if not matched:
        brand = (rec.get("brand") or "").strip().lower()
        if brand in BRAND_DEFAULTS:
            score, material, klass = BRAND_DEFAULTS[brand]
        if (rec.get("gpu") or {}).get("class") == "dedicated" and score < 6.0:
            # a stripped "Gaming Laptop" with an RTX in it is no budget
            # ultrabook: floor it at gaming-plastic level, honestly labelled
            score, material, klass = 6.0, "plastic gaming (unclassified line)", "gaming"

    for rx, delta, word in _MATERIAL_WORDS:
        if rx.search(blob) and word not in material.lower():
            score += delta
            material = f"{material} · {word} mentions"

    label = next((lab for cut, lab in BUILD_LABELS if score >= cut), "Budget")
    return {"score": _clamp(score), "label": label, "material": material, "class": klass}


# ------------------------------------------------------------------- display rating

PANEL_BASE = {
    # OLED sits clearly above everything (infinite contrast) with headroom
    # kept for OLED MacBooks; mini-LED XDR just below; premium LCDs
    # (Retina / PixelSense) a full tier under OLED, not snapping at its heels
    "oled": 9.5, "mini-led": 9.0, "mini led": 9.0, "qled": 7.4,
    "retina": 7.5, "pixelsense": 7.5, "ips": 6.4, "va": 6.2,
    "tn": 3.8, "lcd": 6.0, "": 6.2, "unknown": 6.2,
}

DISPLAY_LABELS = [(9.3, "Exceptional"), (8.5, "Excellent"), (7.5, "Great"),
                  (6.5, "Good"), (5.5, "Average"), (4.5, "Basic")]


def _res_bonus(w: int | None, h: int | None) -> float:
    if not (w and h):
        return 0.0
    px = w * h
    if w <= 1366:
        return -2.0
    if w <= 1600:
        return -0.9
    if px <= 1920 * 1200:
        return 0.0
    if px <= 2240 * 1400:
        return 0.3
    if px <= 2560 * 1600:
        return 0.6
    if px <= 2880 * 1800:
        return 0.9
    return 1.0


def _ppi(rec: dict) -> float | None:
    s = rec.get("screen") or {}
    if not (s.get("width") and s.get("height") and s.get("inches")):
        return None
    return ((s["width"] ** 2 + s["height"] ** 2) ** 0.5) / s["inches"]


def rate_display(rec: dict, build_class: str = "mainstream") -> dict:
    """Return {'score', 'label', 'note', 'kind'} for a record's screen."""
    s = rec.get("screen") or {}
    blob = _blob(rec)
    panel = (s.get("panel") or "").lower()
    base = PANEL_BASE.get(panel, 6.2)
    w, h, hz = s.get("width"), s.get("height"), s.get("hz") or 0
    score = base + _res_bonus(w, h)

    # refresh
    if hz >= 240:
        score += 0.6
    elif hz >= 144:
        score += 0.45
    elif hz >= 120:
        score += 0.35
    elif hz >= 90:
        score += 0.2

    # pixel density
    ppi = _ppi(rec)
    if ppi:
        if ppi >= 220:
            score += 0.3
        elif ppi <= 135:
            score -= 0.2

    # brightness (only when actually mentioned)
    nits = None
    m = re.search(r"(\d{3,4})\s*nits?", blob)
    if m:
        nits = int(m.group(1))
        if nits >= 600:
            score += 0.4
        elif nits >= 400:
            score += 0.25
        elif nits >= 350:
            score += 0.1
        elif nits <= 250:
            score -= 0.15

    # gamut (decimal-aware: "62.5% sRGB", "45% NTSC", "99% DCI-P3" …)
    gamut, gamut_pct = None, None
    m = re.search(r"(\d{2,3}(?:\.\d+)?)\s*%\s*(?:dci[- ]?p3|\bp3\b)", blob)
    if m:
        gamut_pct = float(m.group(1))
        if gamut_pct >= 90:
            gamut = "p3"
            score += 0.4
    else:
        m = re.search(r"(\d{2,3}(?:\.\d+)?)\s*%\s*srgb", blob)
        if m:
            gamut_pct = float(m.group(1))
            if gamut_pct >= 95:
                gamut = "srgb"
                score += 0.25
            elif gamut_pct >= 72:
                gamut = "srgb-mid"
            else:
                gamut = "srgb-low"
                score -= 0.7
        else:
            m = re.search(r"(\d{2,3}(?:\.\d+)?)\s*%\s*ntsc", blob)
            if m:
                gamut_pct = float(m.group(1))
                if gamut_pct >= 70:
                    gamut = "ntsc-wide"
                    score += 0.2
                elif gamut_pct <= 50:
                    gamut = "ntsc45"
                    score -= 0.7
                else:
                    gamut = "ntsc"
                    score -= 0.3
            elif re.search(r"ntsc", blob):
                gamut = "ntsc"
                score -= 0.3

    if re.search(r"\bhdr\b", blob):
        score += 0.25

    # OLED in this market is wide-gamut and 400-nit class by default
    if "oled" in panel:
        if gamut is None:
            score += 0.25
        if nits is None:
            score += 0.15

    # budget consumer LCDs in this market are ~300-nit, 45% NTSC panels
    lcd = panel in ("", "unknown", "ips", "va", "tn", "lcd")
    small = (w or 0) * (h or 0) <= 1920 * 1200
    if lcd and small and not gamut and (nits is None or nits < 350):
        if build_class == "business":
            score -= 0.2
        elif build_class in ("budget", "mainstream", "gaming"):
            score -= 0.5

    if s.get("touch"):
        score += 0.1

    # note
    bits: list[str] = []
    kind = "OLED" if "oled" in panel else (
        "Mini-LED" if "mini" in panel else (
            "Retina" if "retina" in panel else (
                "PixelSense" if "pixelsense" in panel else (
                    panel.upper() if panel in ("ips", "va", "tn", "lcd") else "LCD"))))
    bits.append(kind)
    if s.get("inches"):
        bits.append(f"{s['inches']:g}″")
    if s.get("res"):
        bits.append(s["res"])
    if hz:
        bits.append(f"{hz} Hz")
    if nits:
        bits.append(f"{nits} nits")
    elif "oled" in panel:
        bits.append("~400-nit class")
    elif lcd and small and not gamut and build_class in ("budget", "mainstream", "gaming"):
        bits.append("~300-nit class, 45% NTSC-class colour")
    elif lcd and small and not gamut and build_class == "business":
        bits.append("business LCD, ~62% sRGB-class colour")
    if gamut == "p3":
        bits.append(f"~{gamut_pct:g}% DCI-P3" if gamut_pct else "~100% DCI-P3")
    elif gamut == "srgb":
        bits.append(f"~{gamut_pct:g}% sRGB" if gamut_pct else "~100% sRGB")
    elif gamut == "srgb-mid":
        bits.append(f"{gamut_pct:g}% sRGB (decent)")
    elif gamut == "srgb-low":
        bits.append(f"{gamut_pct:g}% sRGB (narrow)")
    elif gamut == "ntsc-wide":
        bits.append(f"{gamut_pct:g}% NTSC (~100% sRGB class)")
    elif gamut == "ntsc45":
        bits.append("45% NTSC-class colour")
    elif gamut == "ntsc":
        bits.append("NTSC gamut")
    elif "oled" in panel:
        bits.append("wide gamut (~DCI-P3)")
    if "hdr" in blob:
        bits.append("HDR")
    if s.get("touch"):
        bits.append("touch")

    label = next((lab for cut, lab in DISPLAY_LABELS if score >= cut), "Poor")
    brief_bits = [kind]
    if s.get("inches"):
        brief_bits.append(f"{s['inches']:g}″")
    if s.get("res"):
        brief_bits.append(s["res"])
    if hz:
        brief_bits.append(f"{hz} Hz")
    return {"score": _clamp(score), "label": label, "note": " · ".join(bits),
            "brief": " · ".join(brief_bits), "kind": kind, "ppi": round(ppi) if ppi else None,
            "hz": hz or None}


# ------------------------------------------------------------------------- pipeline

def apply_ratings(rec: dict) -> dict:
    """Attach display_* and build_* fields to a normalized record (in place)."""
    build = rate_build(rec)
    disp = rate_display(rec, build["class"])
    rec["build_score"] = build["score"]
    rec["build_label"] = build["label"]
    rec["build_material"] = build["material"]
    rec["build_class"] = build["class"]
    rec["display_score"] = disp["score"]
    rec["display_label"] = disp["label"]
    rec["display_note"] = disp["note"]
    rec["display_brief"] = disp["brief"]
    return rec


if __name__ == "__main__":  # quick sanity check
    import json
    import pathlib
    import sys

    path = pathlib.Path(__file__).resolve().parent.parent / "data" / "unified_laptops.json"
    data = json.loads(path.read_text(encoding="utf-8")) if len(sys.argv) < 2 else json.load(sys.stdin)
    for r in data[:40]:
        b = rate_build(r)
        d = rate_display(r, b["class"])
        print(f"{r['sku']:12s} D {d['score']:>4}/10 {d['label']:<11} {d['note'][:52]:<54} "
              f"B {b['score']:>4}/10 {b['label']:<10} {b['material'][:46]}")
