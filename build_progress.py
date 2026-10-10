#!/usr/bin/env python3
"""Build scrape-progress-over-time dashboard (standalone HTML, never overwrites pareto charts).

Reads:  data/price_history.jsonl  (one row per SKU per day: date/store/sku/price/in_stock)
Writes: reports/scrape_progress.html   (NEW file — existing charts untouched)

Charts (hand-rolled SVG, same dark style as the pareto files, works from file://):
  1. Listings observed per day per store
  2. In-stock listings per day per store
  3. Median price per day per store (SAR; PH stores converted at the pipeline rate)
  4. Daily churn per snapshot transition (new / gone / price-changed / stock-flipped SKUs)
Plus a per-date summary table.
"""
from __future__ import annotations
import json
import statistics
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent
HIST = ROOT / "data" / "price_history.jsonl"
OUT = ROOT / "reports" / "scrape_progress.html"

COLORS = {
    "noon": "#5eead4",
    "eXtra": "#38bdf8",
    "Jarir": "#f59e0b",
    "PCX": "#a78bfa",
    "DataBlitz": "#10b981",
    "Gigahertz": "#f472b6",
    "Amazon": "#64748b",
}


def main() -> int:
    per_day: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    with HIST.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            if not r.get("date") or not r.get("store"):
                continue
            per_day[r["date"]][r["store"]].append(r)

    dates = sorted(per_day)
    stores = sorted({s for d in per_day.values() for s in d},
                    key=lambda s: -sum(len(per_day[d].get(s, [])) for d in dates))

    series = {}   # store -> {n:[], instock:[], med:[]}
    table = []
    for d in dates:
        row = {"date": d, "stores": {}, "n": 0, "instock": 0}
        for s in stores:
            recs = per_day[d].get(s, [])
            n = len(recs)
            ins = sum(1 for r in recs if r.get("in_stock"))
            prices = [r["price"] for r in recs if isinstance(r.get("price"), (int, float))]
            med = round(statistics.median(prices)) if prices else None
            row["stores"][s] = {"n": n, "instock": ins, "med": med}
            row["n"] += n
            row["instock"] += ins
        table.append(row)

    for s in stores:
        series[s] = {
            "n": [per_day[d].get(s, []) and len(per_day[d][s]) or 0 for d in dates],
            "instock": [sum(1 for r in per_day[d].get(s, []) if r.get("in_stock")) for d in dates],
            "med": [row["stores"][s]["med"] for row in table],
        }

    # churn between consecutive snapshots (keyed by (store, sku))
    by_date = {}
    with HIST.open(encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            by_date.setdefault(r["date"], {})[(r.get("store"), r.get("sku"))] = r
    churn = []
    for i in range(1, len(dates)):
        P, C = by_date[dates[i - 1]], by_date[dates[i]]
        pk, ck = set(P), set(C)
        both = pk & ck
        churn.append({
            "to": dates[i],
            "new": len(ck - pk),
            "gone": len(pk - ck),
            "price_chg": sum(1 for k in both if P[k].get("price") != C[k].get("price")),
            "stock_flip": sum(1 for k in both
                               if bool(P[k].get("in_stock")) != bool(C[k].get("in_stock"))),
        })

    payload = {"dates": dates, "stores": stores, "colors": COLORS,
               "series": series, "churn": churn, "table": table,
               "total_rows": sum(r["n"] for r in table)}
    print(f"{len(dates)} snapshots ({dates[0]} → {dates[-1]}), "
          f"{payload['total_rows']} rows, stores: {', '.join(stores)}")
    for c in churn[-5:]:
        print(f"  → {c['to']}: new={c['new']} gone={c['gone']} "
              f"price_chg={c['price_chg']} stock_flip={c['stock_flip']}")

    html = TEMPLATE.replace("__PAYLOAD__", json.dumps(payload, ensure_ascii=False))
    html = html.replace("__DATE_RANGE__", f"{dates[0]} → {dates[-1]}")
    html = html.replace("__TOTAL_ROWS__", f"{payload['total_rows']:,}")
    OUT.write_text(html, encoding="utf-8")
    print(f"wrote {OUT.name} ({OUT.stat().st_size/1024:.0f} KB)")
    return 0


TEMPLATE = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Scrape Progress Over Time — __DATE_RANGE__</title>
<style>
:root { --bg:#0b0f16; --card:#111827; --border:#1e293b; --text:#e5e9f0; --sub:#94a3b8; }
body { font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif; margin:0; background:var(--bg); color:var(--text); }
header { padding:18px 22px; border-bottom:1px solid var(--border); }
h1 { margin:0; font-size:19px; }
p.sub { margin:6px 0 0; color:var(--sub); font-size:13px; line-height:1.5; }
.card { background:var(--card); border:1px solid var(--border); border-radius:10px; padding:14px; margin:12px 22px; }
.card h2 { margin:0 0 4px; font-size:14px; }
.card p.hint { margin:0 0 10px; font-size:12px; color:var(--sub); }
.legend { font-size:12px; color:var(--sub); display:flex; gap:14px; flex-wrap:wrap; margin-bottom:8px; }
.dot { display:inline-block; width:9px; height:9px; border-radius:50%; margin-right:5px; }
svg { width:100%; height:300px; background:#0a0e14; border:1px solid var(--border); border-radius:8px; display:block; }
.tt { position:absolute; display:none; background:#1f2937; border:1px solid #374151; color:#f3f4f6; padding:6px 10px; border-radius:6px; font-size:11px; pointer-events:none; z-index:10; max-width:260px; }
.wrap { position:relative; }
table { border-collapse:collapse; font-size:12px; margin:0 22px 40px; width:calc(100% - 44px); }
th,td { padding:6px 8px; border-bottom:1px solid var(--border); text-align:right; white-space:nowrap; }
th:first-child,td:first-child { text-align:left; }
th { position:sticky; top:0; background:var(--card); color:var(--sub); }
tr:hover td { background:rgba(255,255,255,.03); }
</style></head><body>
<header>
<h1>Scrape Progress Over Time <span style="font-size:12px;color:var(--sub)">__DATE_RANGE__ · __TOTAL_ROWS__ observations</span></h1>
<p class="sub">One row per SKU per day from <b>data/price_history.jsonl</b>. Listings = SKUs observed that day;
in-stock = rows with <b>in_stock</b>; median = median listed price (PH stores converted to SAR by the pipeline).
Churn compares consecutive snapshots by (store, sku). Amazon appears only on 2026-09-13 (one-off pull).
Early-August points are Pricing-Ladder seed rows, not full scrapes.</p>
</header>
<div class="card"><h2>1 · Listings observed per day</h2><p class="hint">SKUs seen in each snapshot, per store.</p>
<div class="legend" id="lg1"></div><div class="wrap"><svg id="c1"></svg><div class="tt" id="t1"></div></div></div>
<div class="card"><h2>2 · In-stock listings per day</h2><p class="hint">Rows with in_stock = true.</p>
<div class="legend" id="lg2"></div><div class="wrap"><svg id="c2"></svg><div class="tt" id="t2"></div></div></div>
<div class="card"><h2>3 · Median listed price per day (SAR)</h2><p class="hint">Median of listed prices per store per snapshot.</p>
<div class="legend" id="lg3"></div><div class="wrap"><svg id="c3"></svg><div class="tt" id="t3"></div></div></div>
<div class="card"><h2>4 · Daily churn per transition</h2><p class="hint">New / gone SKUs and changed rows between consecutive snapshots.</p>
<div class="legend"><span><i class="dot" style="background:#10b981"></i>new</span><span><i class="dot" style="background:#f43f5e"></i>gone</span><span><i class="dot" style="background:#38bdf8"></i>price changed</span><span><i class="dot" style="background:#f59e0b"></i>stock flipped</span></div>
<div class="wrap"><svg id="c4" style="height:260px"></svg><div class="tt" id="t4"></div></div></div>
<table><thead><tr id="th"></tr></thead><tbody id="tb"></tbody></table>
<script>
const D = __PAYLOAD__;
const W=960,H=300,pl=52,pr=16,pt=14,pb=34;
function xPos(i,n){ return pl + i/(Math.max(1,n-1))*(W-pl-pr); }
function drawLines(svg,tt,legId,key,fmt){
  const n=D.dates.length;
  let mx=0;
  D.stores.forEach(s=>D.series[s][key].forEach(v=>{ if(v!=null&&v>mx)mx=v; }));
  mx*=1.08||1;
  const Y=v=>pt+(1-v/mx)*(H-pt-pb);
  let s='';
  for(let g=0;g<=4;g++){ const v=mx/1.08*g/4, y=Y(v);
    s+=`<line x1="${pl}" y1="${y}" x2="${W-pr}" y2="${y}" stroke="#162032"/><text x="${pl-6}" y="${y+4}" fill="#64748b" font-size="10" text-anchor="end">${fmt(v)}</text>`; }
  const step=Math.ceil(n/13);
  D.dates.forEach((d,i)=>{ if(i%step||i===n-1&&n%step===1)return; const x=xPos(i,n);
    s+=`<text x="${x}" y="${H-10}" fill="#64748b" font-size="10" text-anchor="middle">${d.slice(5)}</text>`; });
  D.stores.forEach(st=>{ const c=D.colors[st]||'#94a3b8';
    const pts=D.series[st][key].map((v,i)=>v==null?null:[xPos(i,n),Y(v)]).filter(Boolean);
    if(pts.length>1) s+=`<polyline points="${pts.map(p=>p[0].toFixed(1)+','+p[1].toFixed(1)).join(' ')}" fill="none" stroke="${c}" stroke-width="1.8"/>`;
    D.series[st][key].forEach((v,i)=>{ if(v==null)return;
      s+=`<circle cx="${xPos(i,n)}" cy="${Y(v)}" r="3" fill="${c}" class="nd" data-s="${st}" data-i="${i}" data-k="${key}"/>`; }); });
  svg.setAttribute('viewBox',`0 0 ${W} ${H}`); svg.innerHTML=s;
  document.getElementById(legId).innerHTML=D.stores.map(st=>`<span><i class="dot" style="background:${D.colors[st]}"></i>${st}</span>`).join('');
  svg.querySelectorAll('.nd').forEach(el=>{
    el.addEventListener('mouseenter',()=>{ const st=el.dataset.s,i=+el.dataset.i;
      const v=D.series[st][el.dataset.k][i];
      tt.innerHTML=`<b>${st}</b> · ${D.dates[i]}<br>${fmt(v)}`; tt.style.display='block';
      const r=svg.getBoundingClientRect(),cx=+el.getAttribute('cx')/W*r.width;
      tt.style.left=Math.min(r.width-170,Math.max(8,cx-80))+'px'; tt.style.top='8px'; });
    el.addEventListener('mouseleave',()=>tt.style.display='none'); });
}
const int=v=>Math.round(v).toLocaleString();
drawLines(document.getElementById('c1'),document.getElementById('t1'),'lg1','n',int);
drawLines(document.getElementById('c2'),document.getElementById('t2'),'lg2','instock',int);
drawLines(document.getElementById('c3'),document.getElementById('t3'),'lg3','med',v=>Math.round(v).toLocaleString());
// churn bars
(function(){ const svg=document.getElementById('c4'),tt=document.getElementById('t4');
  const rows=D.churn,mx=Math.max(...rows.flatMap(r=>[r.new,r.gone,r.price_chg,r.stock_flip]),1)*1.1;
  const Y=v=>pt+(1-v/mx)*(H-pt-pb), keys=[['new','#10b981'],['gone','#f43f5e'],['price_chg','#38bdf8'],['stock_flip','#f59e0b']];
  const gw=(W-pl-pr)/rows.length,bw=Math.min(14,(gw-14)/4);
  let s='';
  for(let g=0;g<=4;g++){ const v=mx*g/4,y=Y(v);
    s+=`<line x1="${pl}" y1="${y}" x2="${W-pr}" y2="${y}" stroke="#162032"/><text x="${pl-6}" y="${y+4}" fill="#64748b" font-size="10" text-anchor="end">${Math.round(v)}</text>`; }
  rows.forEach((r,i)=>{ const cx=pl+gw*i+gw/2;
    keys.forEach(([k,c],j)=>{ const x=cx-(2*bw)+j*bw*1.15,y=Y(r[k]);
      s+=`<rect x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${(bw*1.05).toFixed(1)}" height="${(H-pb-y).toFixed(1)}" fill="${c}" opacity=".85" class="br" data-i="${i}"><title>${k}: ${r[k]} (${r.to})</title></rect>`; });
    if(i%Math.ceil(rows.length/13)===0||i===rows.length-1)
      s+=`<text x="${cx}" y="${H-10}" fill="#64748b" font-size="10" text-anchor="middle">${r.to.slice(5)}</text>`; });
  svg.setAttribute('viewBox',`0 0 ${W} ${H}`); svg.innerHTML=s;
  svg.querySelectorAll('.br').forEach(el=>{ el.addEventListener('mouseenter',()=>{ const r=rows[+el.dataset.i];
    tt.innerHTML=`<b>→ ${r.to}</b><br>new: ${r.new} · gone: ${r.gone}<br>price changed: ${r.price_chg} · stock flipped: ${r.stock_flip}`;
    tt.style.display='block'; tt.style.left='8px'; tt.style.top='8px'; });
    el.addEventListener('mouseleave',()=>tt.style.display='none'); });
})();
// table
(function(){ const st=D.stores;
  document.getElementById('th').innerHTML='<th>date</th>'+st.map(s=>`<th>${s} n/in</th>`).join('')+'<th>total</th>';
  document.getElementById('tb').innerHTML=D.table.slice().reverse().map(r=>
    `<tr><td>${r.date}</td>`+st.map(s=>{ const c=r.stores[s]; return `<td>${c.n}/${c.instock}</td>`; }).join('')+`<td><b>${r.n}</b>/${r.instock}</td></tr>`).join('');
})();
</script></body></html>"""

if __name__ == "__main__":
    raise SystemExit(main())
