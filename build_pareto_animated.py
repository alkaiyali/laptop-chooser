#!/usr/bin/env python3
"""Build animated pareto chart (price vs overall_score evolving over snapshots).

Reads:
  data/price_history.jsonl   (per-SKU price + in_stock per day)
  data/unified_laptops.json  (static attrs: overall_score, name, cpu, url …)
Writes (NEW file, existing charts untouched):
  reports/pareto_animated.html

Scope: Jarir + eXtra (the re-scraped stores). X = price (log, fixed axis),
Y = overall_score (static per SKU, fixed axis). Each frame = one snapshot date.
In-stock points are solid, out-of-stock hollow; frontier is over in-stock only.
Thin lines show each SKU's move since the previous frame (like the before/after charts).
"""
from __future__ import annotations
import json
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent
HIST = ROOT / "data" / "price_history.jsonl"
UNI = ROOT / "data" / "unified_laptops.json"
OUT = ROOT / "reports" / "pareto_animated.html"
STORES = ("Jarir", "eXtra")


def main() -> int:
    uni = {r["id"]: r for r in json.loads(UNI.read_text(encoding="utf-8"))}

    # static attrs for every Jarir/eXtra SKU in the fresh unified snapshot
    skus = []
    idx = {}
    for r in uni.values():
        if r.get("store") not in STORES:
            continue
        if not r.get("overall_score") or not r.get("price"):
            continue
        cpu = r.get("cpu") or {}
        idx[r["id"]] = len(skus)
        skus.append({
            "id": r["id"],
            "store": r["store"],
            "name": r.get("short_name") or r.get("name"),
            "overall": r.get("overall_score"),
            "perf": r.get("perf_score"),
            "cpu": cpu.get("text") if isinstance(cpu, dict) else cpu,
            "ram": r.get("ram"), "stor": r.get("storage"),
            "url": r.get("url"),
        })
    print(f"skus with static attrs: {len(skus)}")

    # per-date observations (history key uses original-case store; unified id is lowercased)
    obs: dict[str, dict[int, list]] = defaultdict(dict)
    skipped = 0
    with HIST.open(encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            if r.get("store") not in STORES:
                continue
            uid = f"{r['store'].lower()}:{r.get('sku')}"
            i = idx.get(uid)
            if i is None:
                skipped += 1
                continue
            try:
                p = float(r["price"])
            except (TypeError, ValueError):
                continue
            if p <= 0:
                continue
            obs[r["date"]][i] = [round(p, 2), 1 if r.get("in_stock") else 0]

    dates = sorted(obs)
    frames = [{"date": d, "pts": [[i, *obs[d][i]] for i in sorted(obs[d])]} for d in dates]
    npts = sum(len(f["pts"]) for f in frames)
    print(f"{len(dates)} frames ({dates[0]} → {dates[-1]}), {npts} points, "
          f"{skipped} history rows without unified attrs (gone SKUs)")

    payload = {"dates": dates, "skus": skus, "frames": frames}
    html = TEMPLATE.replace("__PAYLOAD__", json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    html = html.replace("__DATE_RANGE__", f"{dates[0]} → {dates[-1]}")
    OUT.write_text(html, encoding="utf-8")
    print(f"wrote {OUT.name} ({OUT.stat().st_size/1024:.0f} KB)")
    return 0


TEMPLATE = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Pareto Over Time — Jarir + eXtra (__DATE_RANGE__)</title>
<style>
:root{--bg:#0b0f16;--card:#111827;--border:#1e293b;--text:#e5e9f0;--sub:#94a3b8;--accent:#f59e0b;--blue:#38bdf8;--link:#5eead4;--green:#10b981;--red:#f43f5e}
body{font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif;margin:0;background:var(--bg);color:var(--text)}
header{padding:18px 22px;border-bottom:1px solid var(--border)}
h1{margin:0;font-size:19px;display:flex;align-items:center;gap:10px;flex-wrap:wrap}
p.sub{margin:6px 0 0;color:var(--sub);font-size:13px;line-height:1.5}
.controls{display:flex;gap:8px;align-items:center;margin:14px 22px 0;flex-wrap:wrap}
button.tab,.ctl{background:#151e2e;border:1px solid var(--border);color:var(--sub);padding:7px 14px;border-radius:8px;font-size:13px;font-weight:500;cursor:pointer}
button.tab.active{background:#1e293b;color:#fff;border-color:var(--accent);font-weight:600}
.ctl:hover{color:var(--text);border-color:#334155}
input[type=range]{flex:1;min-width:200px;accent-color:var(--accent)}
.datelabel{font-size:14px;font-weight:700;min-width:110px}
.speed{font-size:12px;color:var(--sub)}
.card{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:14px;margin:12px 22px}
.legend{font-size:12px;color:var(--sub);display:flex;gap:16px;flex-wrap:wrap;margin-bottom:10px;align-items:center}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:5px;vertical-align:middle}
.meta{font-size:12px;color:var(--sub);margin-bottom:10px}
.wrap{position:relative;width:100%}
svg{width:100%;height:480px;background:#0a0e14;border:1px solid var(--border);border-radius:8px;display:block}
svg circle{transition:cx .55s ease,cy .55s ease}
.tt{position:absolute;display:none;background:#1f2937;border:1px solid #374151;color:#f3f4f6;padding:8px 12px;border-radius:6px;font-size:11px;pointer-events:none;z-index:10;max-width:280px}
table{border-collapse:collapse;font-size:12px;margin:0 22px 40px;width:calc(100% - 44px)}
th,td{padding:7px 9px;border-bottom:1px solid var(--border);text-align:left;white-space:nowrap}
th{background:var(--card);color:var(--sub)}
td.name{white-space:normal;min-width:200px}
a{color:var(--link);text-decoration:none}
tr.knee td{background:rgba(245,158,11,.08)}
</style></head><body>
<header>
<h1>Pareto Over Time — Jarir + eXtra</h1>
<p class="sub">X = price that day (log scale, fixed axis), Y = overall_score (static per SKU).
Solid blue = in stock that day, hollow = out of stock (frontier is in-stock only).
Thin line = move since previous snapshot (red = hike). Dashed grey = previous-frame frontier, solid orange = current frontier.
Press play or drag the slider. Data: price_history.jsonl + unified_laptops.json.</p>
</header>
<div class="controls">
<button class="tab active" data-store="both">Jarir+eXtra</button>
<button class="tab" data-store="Jarir">Jarir</button>
<button class="tab" data-store="eXtra">eXtra</button>
<button class="ctl" id="play">▶ Play</button>
<button class="ctl" id="prev">◀</button>
<button class="ctl" id="next">▶</button>
<input type="range" id="slider" min="0" value="0">
<span class="datelabel" id="datelabel"></span>
<span class="speed">speed <select id="speed"><option value="1400">slow</option><option value="800" selected>normal</option><option value="350">fast</option></select></span>
</div>
<div class="card">
<div class="legend"><span><i class="dot" style="background:#38bdf8"></i>in stock</span>
<span><i class="dot" style="background:none;border:2px solid #64748b"></i>out of stock</span>
<span><i class="dot" style="background:#f59e0b"></i>frontier</span>
<span style="color:#94a3b8">— dashed: previous-frame frontier</span></div>
<div class="meta" id="meta"></div>
<div class="wrap"><svg id="svg"></svg><div class="tt" id="tt"></div></div>
</div>
<table><thead><tr><th>#</th><th>Frontier laptop</th><th>Store</th><th>Price</th><th>Prev</th><th>Delta</th><th>Overall</th></tr></thead>
<tbody id="tb"></tbody></table>
<script>
const D=__PAYLOAD__;
const W=960,H=480,pl=50,pr=20,pt=16,pb=36;
let store='both',fi=0,timer=null;
const svg=document.getElementById('svg'),tt=document.getElementById('tt'),
meta=document.getElementById('meta'),tb=document.getElementById('tb'),
slider=document.getElementById('slider'),datelabel=document.getElementById('datelabel'),
playBtn=document.getElementById('play');
slider.max=D.frames.length-1;
// fixed axes over all frames+skus
let allP=[];D.frames.forEach(f=>f.pts.forEach(p=>allP.push(p[1])));
const lmin=Math.log(Math.min(...allP)*0.9),lmax=Math.log(Math.max(...allP)*1.08);
const allO=D.skus.map(s=>s.overall);
const ymin=Math.max(10,Math.floor((Math.min(...allO)-5)/10)*10),ymax=100;
const X=p=>pl+(Math.log(p)-lmin)/(lmax-lmin)*(W-pl-pr);
const Y=o=>pt+(1-(o-ymin)/(ymax-ymin))*(H-pt-pb);
const keep=i=>store==='both'||D.skus[i].store===store;
function frontierOf(pts){ // pts: [[i,price,stock]]
  const s=pts.filter(p=>p[2]===1&&keep(p[0])).sort((a,b)=>a[1]-b[1]||D.skus[b[0]].overall-D.skus[a[0]].overall);
  const f=[];let m=-1;for(const p of s){const o=D.skus[p[0]].overall;if(o>m){f.push(p);m=o;}}
  return f;
}
function render(){
  const F=D.frames[fi],prev=fi>0?D.frames[fi-1]:null;
  const prevPrice=new Map(prev?prev.pts.map(p=>[p[0],p[1]]):[]);
  const vis=F.pts.filter(p=>keep(p[0]));
  const inStk=vis.filter(p=>p[2]===1).length;
  const setA=new Set(frontierOf(F.pts).map(p=>p[0]));
  const setB=new Set(fi>0?frontierOf(prev.pts).map(p=>p[0]):[]);
  let s='';
  [1500,2000,2500,3000,4000,5000,7000,10000,15000,20000,30000].forEach(p=>{
    if(p<Math.min(...allP)*0.9||p>Math.max(...allP)*1.08)return;const x=X(p);
    s+=`<line x1="${x}" y1="${pt}" x2="${x}" y2="${H-pb}" stroke="#1e293b"/><text x="${x}" y="${H-14}" fill="#64748b" font-size="11" text-anchor="middle">${p>=1000?(p/1000)+'k':p}</text>`;});
  for(let o=ymin;o<=ymax;o+=10){const y=Y(o);
    s+=`<line x1="${pl}" y1="${y}" x2="${W-pr}" y2="${y}" stroke="#162032"/><text x="${pl-8}" y="${y+4}" fill="#64748b" font-size="11" text-anchor="end">${o}</text>`;}
  // move lines vs previous frame
  vis.forEach(p=>{ if(!prevPrice.has(p[0]))return;const a=prevPrice.get(p[0]);if(Math.abs(a-p[1])<0.5)return;
    const o=D.skus[p[0]].overall;
    s+=`<line x1="${X(a)}" y1="${Y(o)}" x2="${X(p[1])}" y2="${Y(o)}" stroke="${p[1]<=a?'#475569':'#e11d48'}" stroke-width="1.2" opacity=".7"/>`;});
  function path(set,frame){const f=[...frame.pts].filter(p=>set.has(p[0])).sort((a,b)=>a[1]-b[1]);
    if(!f.length)return'';return `<polyline points="${f.map(p=>X(p[1]).toFixed(1)+','+Y(D.skus[p[0]].overall).toFixed(1)).join(' ')}" fill="none" stroke="COLOR" stroke-width="2.2" DASH/>`;}
  if(fi>0)s+=path(setB,prev).replace('COLOR','#64748b').replace('DASH','stroke-dasharray="6 4"');
  s+=path(setA,F).replace('COLOR','#f59e0b').replace('DASH','');
  vis.forEach(p=>{const sk=D.skus[p[0]];
    if(p[2]===1){const f=setA.has(p[0]);
      s+=`<circle cx="${X(p[1])}" cy="${Y(sk.overall)}" r="${f?5.5:3.8}" fill="${f?'#f59e0b':'#38bdf8'}" class="nd" data-i="${p[0]}"/>`;}
    else s+=`<circle cx="${X(p[1])}" cy="${Y(sk.overall)}" r="3.2" fill="none" stroke="#64748b" stroke-width="1.4" class="nd" data-i="${p[0]}"/>`;});
  svg.setAttribute('viewBox',`0 0 ${W} ${H}`);svg.innerHTML=s;
  // moved count
  let moved=0;vis.forEach(p=>{if(prevPrice.has(p[0])&&Math.abs(prevPrice.get(p[0])-p[1])>0.5)moved++;});
  datelabel.textContent=F.date;
  meta.textContent=`${F.date} · ${vis.length} shown (${inStk} in stock) · frontier ${setA.size} · ${moved} price moves vs ${prev?prev.date:'—'}`;
  slider.value=fi;
  const look=new Map(vis.map(p=>[p[0],p]));
  svg.querySelectorAll('.nd').forEach(el=>{el.addEventListener('mouseenter',()=>{const i=+el.dataset.i,sk=D.skus[i],p=look.get(i);
    const pv=prevPrice.has(i)?prevPrice.get(i):null;
    const d=pv==null?'new this snapshot':(p[1]>pv?`hike +${(p[1]-pv).toFixed(0)}`:(p[1]<pv?`drop ${(p[1]-pv).toFixed(0)}`:'unchanged'));
    tt.innerHTML=`<b>${sk.name}</b> (${sk.store})<br>${sk.cpu||''}<br>${F.date}: <b>${p[1].toFixed(0)} SAR</b> ${p[2]?'':'(OOS)'}<br>prev: ${pv==null?'—':pv.toFixed(0)} · ${d}<br>Overall <b>${sk.overall}</b>`;
    tt.style.display='block';const r=svg.getBoundingClientRect(),cx=+el.getAttribute('cx')/W*r.width;
    tt.style.left=Math.min(r.width-280,Math.max(8,cx-130))+'px';tt.style.top='8px';});
    el.addEventListener('mouseleave',()=>tt.style.display='none');
    el.addEventListener('click',()=>{const sk=D.skus[+el.dataset.i];if(sk.url)window.open(sk.url,'_blank');});});
  const rows=[...vis].filter(p=>setA.has(p[0])).sort((a,b)=>a[1]-b[1]);
  tb.innerHTML=rows.map((p,k)=>{const sk=D.skus[p[0]],pv=prevPrice.get(p[0]);
    const dd=pv==null?'<span style="color:#94a3b8">new</span>':(Math.abs(p[1]-pv)<0.5?'<span style="color:#64748b">—</span>':(p[1]<pv?`<span style="color:var(--green)">${(p[1]-pv).toFixed(0)}</span>`:`<span style="color:var(--red)">+${(p[1]-pv).toFixed(0)}</span>`));
    return `<tr class="knee"><td>${k+1}</td><td class="name"><a href="${sk.url}" target="_blank">${sk.name}</a></td><td>${sk.store}</td><td><b>${p[1].toFixed(0)}</b></td><td>${pv==null?'—':pv.toFixed(0)}</td><td>${dd}</td><td><b>${sk.overall}</b></td></tr>`;}).join('');
}
function step(d){fi=(fi+d+D.frames.length)%D.frames.length;render();}
function play(){if(timer){clearInterval(timer);timer=null;playBtn.textContent='▶ Play';}
  else{timer=setInterval(()=>{fi=(fi+1)%D.frames.length;render();},+document.getElementById('speed').value);playBtn.textContent='⏸ Pause';}}
playBtn.onclick=play;document.getElementById('prev').onclick=()=>step(-1);document.getElementById('next').onclick=()=>step(1);
slider.oninput=()=>{fi=+slider.value;render();};
document.querySelectorAll('.tab').forEach(b=>b.onclick=()=>{document.querySelectorAll('.tab').forEach(x=>x.classList.remove('active'));b.classList.add('active');store=b.dataset.store;render();});
render();
</script></body></html>"""

if __name__ == "__main__":
    raise SystemExit(main())
