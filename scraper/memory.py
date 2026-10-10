"""RAM upgrade options: slots, soldered memory and the practical maximum.

None of the four stores publishes memory-slot information — Jarir and eXtra list
"RAM Type" only, noon and Amazon-style feeds nothing at all.  So this is inferred
from the model line, exactly the way the Pricing Ladder research worked it out by
hand:

* **documented** — the families the ladder pinned down (Vivobook 16 = 16 GB soldered
  + one free slot / 32 GB ceiling, Vivobook S16 = 8 + 8 both occupied / 16 GB,
  ProBook 4 = 2 × 12 full / 48 GB, IdeaPad Slim 5 = two SO-DIMMs / 32 GB, Zenbook and
  every MacBook soldered).
* **typical** — otherwise the class rule: premium ultrabooks are soldered LPDDR,
  business lines have two SO-DIMMs, gaming lines have two, budget consumer lines one.

Fields written per record: ``ram_max_gb``, ``ram_slots``, ``ram_soldered``,
``ram_note`` (one-line explanation) and ``ram_source``.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# (regex on the product name, slots, soldered GB, max GB, note, source)
# slots = SO-DIMM/DIMM sockets the chassis has (0 = soldered only)
# soldered = GB permanently on the board (None = none)
# max GB = practical ceiling the vendor documents / the class supports
FAMILIES: list[tuple[str, int | None, int | None, int | None, str, str]] = [
    # --- documented by the Pricing Ladder research -------------------------
    (r"macbook", 0, None, None,
     "Memory is soldered to the logic board — you pick the capacity when you buy; "
     "nothing can be added later (8/16/24/32/36/48 GB depending on chip)", "documented"),
    (r"zenbook", 0, None, None,
     "Soldered LPDDR5x — capacity is fixed at purchase (the A14 comes sealed at 16 GB)", "documented"),
    (r"vivobook s ?1[456]", 1, 8, 16,
     "8 GB soldered plus one SO-DIMM slot, usually already holding 8 GB — 16 GB is the "
     "ceiling either way, so buy the capacity you need", "documented"),
    (r"x1607", 1, 16, 32,
     "16 GB soldered with one free SO-DIMM slot — one 16 GB stick takes it to 32 GB "
     "(the easiest upgrade in the budget tier)", "documented"),
    (r"probook", 2, 0, 48,
     "Two SO-DIMM slots; HP ships them full (2 × 12 GB in the ProBook 4) — replacing "
     "both sticks is what raises the ceiling to 48 GB", "documented"),
    (r"ideapad slim 5|ideapad pro", 2, 0, 32,
     "Two removable SO-DIMM slots shipped as 2 × 8 GB — an ordinary 2 × 16 GB kit "
     "reaches 32 GB", "documented"),
    (r"omnibook", 0, None, None,
     "Soldered LPDDR5/LPDDR5X (on-board) — capacity is fixed at purchase, no slots "
     "(HP Maintenance & Service Guides for 14-fp/14-hk/14-hh/14-hm/14-km/16-af: "
     "\"onboard, not accessible or upgradeable\")", "documented"),
    (r"matebook d", 0, None, None,
     "Soldered memory — capacity fixed at purchase, no slots (Notebookcheck D 16 2024 "
     "teardown: \"16 GB of soldered RAM\" with room for sockets left empty; GSMArena: "
     "\"RAM is soldered onto the motherboard and is not upgradeable\"; Huawei and "
     "TechRadar spec LPDDR4X — ignoring the DDR4/DDR5 words in marketplace titles)", "documented"),
    # --- typical for the class --------------------------------------------
    (r"ideapad slim 3|ideapad slim 1|ideapad 1|ideapad 3", 1, 8, 16,
     "8 GB soldered plus one SO-DIMM slot — 16 GB ceiling", "typical"),
    (r"ideapad gaming|ideapad pro gaming", 2, 0, 32, "Two SO-DIMM slots", "typical"),
    (r"ideapad|yoga slim|yoga ", 0, None, None,
     "Soldered LPDDR5 memory — capacity fixed at purchase", "typical"),
    (r"thinkpad (x1|x9)", 0, None, None, "Soldered LPDDR5x — fixed at purchase", "typical"),
    (r"thinkpad (t|p|x)\d", 1, 8, 64,
     "8–16 GB soldered plus one SO-DIMM slot", "typical"),
    (r"thinkpad (e|l)\d", 2, 0, 64, "Two SO-DIMM slots (some SKUs ship one soldered + one slot)", "typical"),
    (r"thinkbook", 2, 0, 32, "Two SO-DIMM slots", "typical"),
    (r"legion|loq", 2, 0, 64, "Two SO-DIMM slots (both reachable under the bottom cover)", "typical"),
    (r"elitebook|zbook", 2, 0, 64, "Two SO-DIMM slots", "typical"),
    (r"pro ?book|pro max", 2, 0, 64, "Two SO-DIMM slots", "typical"),
    (r"xps 1[345]", 0, None, None, "Soldered LPDDR5 — fixed at purchase", "typical"),
    (r"xps", 2, 0, 64, "Two SO-DIMM slots", "typical"),
    (r"latitude 9|latitude 7|latitude 5|latitude 3|precision", 2, 0, 64,
     "Two SO-DIMM slots", "typical"),
    (r"surface", 0, None, None, "Soldered LPDDR — fixed at purchase", "typical"),
    (r"swift", 0, None, None, "Soldered LPDDR5x — fixed at purchase", "typical"),
    (r"galaxy book.*(pro|ultra|edge)", 0, None, None, "Soldered LPDDR — fixed at purchase", "typical"),
    (r"galaxy book", 1, 8, 24, "8 GB soldered plus one SO-DIMM slot", "typical"),
    (r"matebook x pro|matebook 14|matebook 16", 0, None, None, "Soldered LPDDR — fixed at purchase", "typical"),
    (r"magicbook", 0, None, None, "Soldered LPDDR — fixed at purchase", "typical"),
    (r"aspire lite|aspire 3|aspire 5", 1, 8, 24, "8 GB soldered plus one SO-DIMM slot", "typical"),
    (r"aspire", 1, 8, 32, "8 GB soldered plus one SO-DIMM slot", "typical"),
    (r"nitro|predator|cyborg|katana|thin|stealth|raider|vector|titan|modern|sword|pulse|bravo",
     2, 0, 64, "Two SO-DIMM slots", "typical"),
    (r"tuf|rog|zephyrus|strix|omen|victus|pavilion gaming|inspiron 1[45]|g1[56]|alienware",
     2, 0, 64, "Two SO-DIMM slots", "typical"),
    (r"pavilion|envy|probook", 2, 0, 32, "Two SO-DIMM slots (some SKUs one + one)", "typical"),
    (r"hp (laptop|15|17|240|250|255)|pavilion", 2, 0, 32, "Two SO-DIMM slots", "typical"),
    (r"infiniarc|tecno|infinix|positivo|winfun|colorful", 1, 8, 16, "8 GB soldered plus one SO-DIMM slot", "typical"),
    # remaining common families seen in the KSA/noon catalogues
    (r"x1605", 1, None, 24,
     "One SO-DIMM slot, 24 GB ceiling: ASUS specs the X1605VA at 8 GB onboard "
     "on some SKUs (e.g. the Core 9 270H 8+8 GB) and 16 GB on others — the slot "
     "takes either to 24 GB", "typical"),
    (r"vivobook", 1, 8, 16, "8 GB soldered plus one SO-DIMM slot (Vivobook Go/14/15 class)", "typical"),
    (r"dell pro|\bpro (1[0-9]|max|premium)\b|pro 15|pro 14|pro 16", 2, 0, 64,
     "Two SO-DIMM slots (Dell's Pro line replaced the Latitude 3000/5000 series)", "typical"),
    (r"a3\d{2}|a5\d{2}|a1\d{2}|extensa|travelmate", 1, 8, 24,
     "8 GB soldered plus one SO-DIMM slot", "typical"),
    (r"gigabyte|aero|aorus|g5 |g6 |g7 ", 2, 0, 64, "Two SO-DIMM slots", "typical"),
    (r"\bmsi\b|gf\d{2}|gp\d{2}|ge\d{2}|gl\d{2}|sword|pulse|bravo|creator", 2, 0, 64,
     "Two SO-DIMM slots", "typical"),
    (r"lenovo v\d|lenovo (1|3)\d\d|v1[45] |v15", 1, 8, 16, "8 GB soldered plus one SO-DIMM slot", "typical"),
    (r"infiniarc", 2, 0, 32, "Two SO-DIMM slots", "typical"),
]


def _installed_note(rec: dict) -> str:
    ram = rec.get("ram")
    ram_type = rec.get("ram_type") or ""
    return f"{ram} GB {ram_type}".strip() if ram else (ram_type or "")


# labels Lenovo/HP/ASUS/MSI use in the spec table DataBlitz copies into the description
_EVIDENCE_LABELS = {
    "memory", "memory slots", "memory slot", "max memory", "maximum memory",
    "memory slot and max capacity", "so-dimm sockets for expansion",
    "memory size", "memory type", "memory form factor", "memory layout",
}


def parse_ram_evidence(description: str | None) -> dict | None:
    """Read real slot / ceiling facts out of a vendor spec table.

    DataBlitz copies the manufacturer's own table into the product description
    ("Memory: 16GB Soldered LPDDR5-5500", "Memory Slots: Memory soldered to
    systemboard, no slots", "Max Memory: 16GB soldered memory, not upgradable",
    "Memory Slot and Max Capacity: DDR4-3200, 2 Slots, Max 64GB").  That is hard
    evidence, so it overrides the family guess.
    """
    lines = [line.strip() for line in (description or "").splitlines()]
    values: dict[str, str] = {}
    for i, line in enumerate(lines):
        key = line.lower().rstrip(":").strip()
        if key in _EVIDENCE_LABELS:
            j = i + 1
            while j < len(lines) and not lines[j]:
                j += 1
            if j < len(lines):
                values.setdefault(key, lines[j].strip())
    if not values:
        return None

    mem = values.get("memory", "")
    slots_text = " ".join(filter(None, [
        values.get("memory slots", ""), values.get("memory slot", ""),
        values.get("memory slot and max capacity", ""),
        values.get("so-dimm sockets for expansion", ""), values.get("memory layout", "")]))
    max_text = " ".join(filter(None, [
        values.get("max memory", ""), values.get("maximum memory", ""),
        values.get("memory slot and max capacity", "")]))
    blob = f"{mem} {slots_text} {max_text}".lower()

    # slots
    slots: int | None = None
    m = (re.search(r"\b(\d)\s*x?\s*[\w.\- ]{0,18}?(?:slots?|sockets?)\b", blob)
         or re.search(r"\b(one|two|three|four)\s+[\w.\- ]{0,18}?(?:slots?|sockets?)\b", blob))
    if m:
        word = m.group(1)
        slots = {"one": 1, "two": 2, "three": 3, "four": 4}.get(word, None) or int(word)
    elif re.search(r"no slots|soldered to (?:the )?system ?board|not upgradable|soldered memory", blob):
        slots = 0
    elif re.search(r"so-?dimm\s*sockets? for expansion", blob):
        slots = 2 if "sockets" in blob else 1
    elif re.search(r"\bslots\b", slots_text.lower()):
        slots = 2 if re.search(r"slots", slots_text.lower()) else 1
    elif re.search(r"\bsockets?\b", slots_text.lower()):
        slots = 2 if re.search(r"sockets", slots_text.lower()) else 1


    # ceiling
    max_gb: int | None = None
    m = (re.search(r"max(?:imum)?[^\d]{0,12}(\d{2,3})\s?gb", max_text, re.I)
         or re.search(r"(\d{2,3})\s?gb", max_text, re.I))
    if m:
        max_gb = int(m.group(1))
    if max_gb is None and slots == 0:
        m = re.search(r"(\d{1,3})\s?gb", mem, re.I)
        if m:
            max_gb = int(m.group(1))          # soldered: what you bought is the ceiling

    # how much is soldered, and the stick layout
    soldered: int | None = None
    m = re.search(r"(\d{1,3})\s?gb[^,;/]*soldered|soldered[^,;/]*?(\d{1,3})\s?gb", mem, re.I)
    if m:
        soldered = int(m.group(1) or m.group(2))
    elif "onboard" in mem.lower() or "on board" in mem.lower():
        m = re.search(r"(\d{1,3})\s?gb", mem, re.I)
        if m:
            soldered = int(m.group(1))
    sticks = None
    m = re.search(r"(\d)\s*[x×]\s*(\d{1,3})\s?gb", mem, re.I)
    if m:
        sticks = f"{m.group(1)} × {m.group(2)}GB"
    else:
        m = re.search(r"(\d{1,3})\s?gb\s*[x×]\s*(\d)", mem, re.I)
        if m:
            sticks = f"{m.group(2)} × {m.group(1)}GB"

    if slots is None and max_gb is None and soldered is None and not sticks:
        return None
    return {"slots": slots, "soldered": soldered, "max_gb": max_gb, "sticks": sticks,
            "evidence": " / ".join(f"{k}: {v}" for k, v in values.items())[:200]}


def memory_options(rec: dict) -> dict | None:
    """Return {slots, soldered, max_gb, note, source} for a record, or None."""
    name = re.sub(r"\s+", " ", (rec.get("brand") or "") + " " + (rec.get("model") or "") + " "
                  + (rec.get("name") or "")).strip().lower()
    # ASUS Vivobook 15 X1504 — verified against ASUS spec sheets.  Memory type
    # is a per-SKU fact (identical X1504VA suffixes ship DDR4 and DDR5 boards),
    # so it is NEVER inferred from the CPU.  The model code decides the slot
    # layout: X1504VA = 1× DDR4 SO-DIMM, X1504VAP(F) = 2× DDR4 SO-DIMM,
    # X1504VAPB = 2× DDR5 SO-DIMM, X1504MA = 1× DDR5 SO-DIMM.  An unnamed
    # listing could be any of them, so its layout stays unknown and the ceiling
    # is the conservative 16GB (never the DDR5 24/32).
    intel_cpu = re.search(r"intel|core|ultra|\bi[3579][-\s]",
                          (rec.get("cpu") or {}).get("text") or "", re.I)
    if re.search(r"x1504", name) or (intel_cpu and re.search(r"vivobook 15\b", name)):
        blob = " ".join([rec.get("model_code") or "", rec.get("model") or "", rec.get("name") or ""])
        code, slots = None, None
        for cand, n_slots in (("VAPB", 2), ("VAPF", 2), ("VAP", 2), ("MA", 1), ("VA", 1)):
            if re.search(r"x1504" + cand + r"\b", blob, re.I):
                code, slots = cand, n_slots
                break
        rt = (rec.get("ram_type") or "").upper()
        ddr5 = "DDR5" in rt or code in ("VAPB", "MA")
        if not code:                       # layout not in the listing: stay conservative
            return {"slots": None, "soldered": None,
                    "max_gb": max(16, rec.get("ram") or 0),
                    "note": "memory layout not stated in the listing — conservative 16GB ceiling "
                            "(ASUS X1504 ships 1 or 2 SO-DIMM slots depending on the model code)",
                    "source": "documented"}
        max_gb = 24 if ddr5 else 16
        return {"slots": slots, "soldered": 8, "max_gb": max(max_gb, rec.get("ram") or 0),
                "note": f"ASUS X1504{code} layout: {slots}× {'DDR5' if ddr5 else 'DDR4'} SO-DIMM"
                        + (" (8GB onboard)" if slots == 1 else "")
                        + f" — {max_gb}GB ceiling",
                "source": "documented"}
    for pattern, slots, soldered, max_gb, note, source in FAMILIES:
        if re.search(pattern, name):
            return {"slots": slots, "soldered": soldered, "max_gb": max_gb,
                    "note": note, "source": source}
    return None


def _display(rec: dict, info: dict) -> str | None:
    """Compact summary for the card row, e.g. "2 SO-DIMM slots · max 32GB"."""
    ram, slots, max_gb = rec.get("ram"), info["slots"], info["max_gb"]
    if slots == 0:
        return "soldered — no upgrade"
    parts = []
    if slots:
        parts.append(f"{slots} SO-DIMM slot{'s' if slots > 1 else ''}")
    soldered = info.get("soldered")
    if soldered:
        fitted = (ram - soldered) if (ram and ram > soldered) else 0
        parts.append(f"{soldered}GB soldered" + (f" + {fitted}GB fitted" if fitted else ""))
    if max_gb:
        full = bool(ram and ram >= max_gb)
        parts.append(f"{max_gb}GB max" + (" (reached)" if full else ""))
    elif info.get("sticks"):
        parts.append(f"{info['sticks']} fitted, ceiling not listed")
    if not parts:
        return None
    return " · ".join(parts)


def _apply(rec: dict, slots, soldered, max_gb, source: str, note: str, evidence: str = "") -> None:
    rec["ram_slots"] = slots
    rec["ram_soldered"] = soldered
    rec["ram_max_gb"] = max_gb
    rec["ram_source"] = source
    rec["ram_note"] = _display(rec, {"slots": slots, "soldered": soldered, "max_gb": max_gb,
                                     "sticks": None})
    rec["ram_detail"] = (f"{note} — from the manufacturer's spec sheet: {evidence}"
                         if evidence else note)


def apply_memory(records: list[dict], descriptions: dict[str, str] | None = None) -> dict:
    """Attach RAM upgrade fields, preferring hard evidence over class heuristics.

    Order of authority:

    1. ``verified`` — the vendor's own memory spec table in the product description
       (DataBlitz copies Lenovo/HP/MSI/ASUS sheets verbatim: "Memory Slots: One DDR5
       SODIMM slot", "Max Memory: 32GB").
    2. ``verified`` (same model) — an identical configuration sold elsewhere inherits
       that evidence; the chassis and its sockets are the same machine.
    3. ``documented`` — the families the Pricing Ladder research pinned down.
    4. ``typical`` — the class rule (premium soldered, business/gaming two slots …),
       explicitly marked as an estimate in the viewer.
    """
    descriptions = descriptions or {}
    stats = {"verified": 0, "verified_same_model": 0, "documented": 0, "typical": 0, "unknown": 0}

    def note_for(rec, slots, soldered, max_gb, sticks=None):
        return _display(rec, {"slots": slots, "soldered": soldered, "max_gb": max_gb, "sticks": sticks})

    # 1. hard evidence -------------------------------------------------------
    for rec in records:
        ev = parse_ram_evidence(descriptions.get(rec.get("sku") or ""))
        if not ev:
            continue
        if ev["max_gb"] is None and (ev["slots"] or 0) > 0:      # only when expansion exists
            fam = memory_options(rec)
            if fam and fam["source"] == "documented" and fam["max_gb"]:
                ev["max_gb"] = fam["max_gb"]
                ev["evidence"] = f"{ev['evidence']} · ceiling per the family rule: {fam['max_gb']}GB"
        complete = ev["slots"] is not None or ev["max_gb"] is not None
        note = note_for(rec, ev["slots"], ev["soldered"], ev["max_gb"], ev["sticks"])
        if not complete:
            if ev.get("sticks"):
                note = f"{ev['sticks']} fitted — ceiling not in the spec sheet"
            elif ev.get("soldered"):
                note = f"{ev['soldered']}GB soldered — ceiling not in the spec sheet"
        if not note:
            continue
        rec["ram_slots"] = ev["slots"] if complete else None
        rec["ram_soldered"] = ev["soldered"]
        rec["ram_max_gb"] = ev["max_gb"] if complete else None
        rec["ram_note"] = note
        rec["ram_source"] = "verified"
        rec["ram_detail"] = ("Manufacturer memory spec — from the spec sheet: " + ev["evidence"])
        stats["verified"] += 1

    # 2. same model, other store --------------------------------------------
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from dedupe import config_key  # local import to avoid a circular import at load time
        verified: dict[tuple, dict] = {}
        for rec in records:
            if rec.get("ram_source") == "verified" and rec.get("ram_max_gb") is not None:
                key = config_key(rec)
                if key and key not in verified:
                    verified[key] = rec
        for rec in records:
            if rec.get("ram_source"):
                continue
            key = config_key(rec)
            src = verified.get(key) if key else None
            if not src:
                continue
            rec["ram_slots"] = src["ram_slots"]
            rec["ram_max_gb"] = src["ram_max_gb"]
            rec["ram_soldered"] = None
            rec["ram_note"] = note_for(rec, src["ram_slots"], None, src["ram_max_gb"])
            rec["ram_source"] = "verified"
            rec["ram_detail"] = (f"Same machine as {src['store']} listing {src['sku']}, whose "
                                 f"manufacturer spec sheet states: {src.get('ram_detail', '')[:120]}")
            stats["verified_same_model"] += 1
    except Exception as err:  # noqa: BLE001
        print(f"[memory] cross-store propagation skipped: {err}")

    # 3. documented families, then the class rule ---------------------------
    for rec in records:
        if rec.get("ram_source"):
            continue
        info = memory_options(rec)
        if not info:
            continue
        note = note_for(rec, info["slots"], info["soldered"], info["max_gb"])
        if not note:
            continue
        rec["ram_slots"] = info["slots"]
        rec["ram_soldered"] = info["soldered"]
        rec["ram_max_gb"] = info["max_gb"]
        rec["ram_note"] = note
        rec["ram_source"] = info["source"]
        rec["ram_detail"] = info["note"] + (" — verified from the manufacturer's spec sheet"
                                            if info["source"] == "documented"
                                            else " — typical for this class, not verified")
        stats[info["source"]] += 1

    # 4. LPDDR is physically soldered on every laptop — the class rules
    #    occasionally give it SO-DIMM slots when a title misstates the memory
    #    type.  A machine whose memory type is LPDDR cannot have sockets,
    #    whatever the title says.
    lpddr_fixed = 0
    for rec in records:
        if not (rec.get("ram_type") or "").upper().startswith("LPDDR"):
            continue
        if rec.get("ram_slots") == 0 and rec.get("ram_max_gb") is None:
            continue
        rec["ram_slots"] = 0
        rec["ram_soldered"] = rec.get("ram")
        rec["ram_max_gb"] = None
        rec["ram_note"] = "soldered — no upgrade"
        rec["ram_source"] = rec.get("ram_source") or "verified"
        rec["ram_detail"] = ((rec.get("ram_detail") or "") +
                             " · LPDDR memory is soldered on-board (no slots)").strip(" ·")
        lpddr_fixed += 1
    stats["lpddr_soldered"] = lpddr_fixed

    stats["unknown"] = sum(1 for r in records if not r.get("delisted") and not r.get("ram_note"))
    return stats


if __name__ == "__main__":  # sanity check on the ladder machines
    import json
    import pathlib

    path = pathlib.Path(__file__).resolve().parent.parent / "data" / "unified_laptops.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    apply_memory(data)
    for rec in data:
        if rec["sku"] in ("100521063", "100521060", "MP00049893", "676987", "MP00050513", "668109"):
            print(f"{rec['sku']:12s} {str(rec['ram']):>3} GB  {rec.get('ram_note','—'):42s} "
                  f"[{rec.get('ram_source')}] {rec['name'][:44]}")
