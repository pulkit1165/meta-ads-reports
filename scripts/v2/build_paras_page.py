#!/usr/bin/env python3
"""
build_paras_page.py — the Paras Video Report dashboard page.

Self-contained HTML with the video data embedded, deployed alongside the
Antariksh dashboard. Summary blocks on top, then filter controls (category,
ROAS band, search), then a grid of video cards with thumbnail previews that
open the Facebook video on click, and a per-video breakdown of its campaign
deployments and survival.

Usage:
  python3 scripts/v2/build_paras_page.py \
      --data antariksh/paras_videos.json --out antariksh/paras-videos.html
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

CSS = """
*{box-sizing:border-box}
body{font-family:-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;margin:0;
     background:#f4f1ea;color:#2a2320}
.wrap{max-width:1240px;margin:0 auto;padding:20px 16px 60px}
h1{font-size:20px;margin:0 0 2px;color:#3a2d1f}
.sub{font-size:13px;color:#8a7d6b;margin-bottom:18px}
.blocks{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:16px}
.block{background:#fff;border-radius:12px;padding:16px 18px;box-shadow:0 1px 3px rgba(60,40,20,.08)}
.block .k{font-size:11px;color:#9a8d7b;text-transform:uppercase;letter-spacing:.05em;font-weight:600}
.block .v{font-size:26px;font-weight:700;color:#3a2d1f;margin-top:4px}
.block .s{font-size:12px;color:#8a7d6b;margin-top:2px}
.cats{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:16px}
.catchip{background:#fff;border-radius:20px;padding:7px 14px;font-size:12px;font-weight:600;
         cursor:pointer;border:1.5px solid transparent;box-shadow:0 1px 2px rgba(60,40,20,.06);
         display:flex;gap:7px;align-items:center;color:#5a4d3b}
.catchip.on{border-color:#b08d57;background:#fdf8ef}
.catchip .n{color:#9a8d7b;font-weight:500}
.bar{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin-bottom:16px}
.bar input,.bar select{padding:9px 12px;border:1px solid #ddd2c0;border-radius:9px;font-size:13px;
                       background:#fff;color:#3a2d1f}
.bar input{flex:1;min-width:180px}
.bar label{font-size:12px;color:#8a7d6b;font-weight:600}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:14px}
.card{background:#fff;border-radius:12px;overflow:hidden;box-shadow:0 1px 3px rgba(60,40,20,.09);
      cursor:pointer;transition:transform .1s,box-shadow .1s}
.card:hover{transform:translateY(-2px);box-shadow:0 6px 18px rgba(60,40,20,.14)}
.thumb{position:relative;aspect-ratio:9/12;background:#e8e0d2 center/cover no-repeat;display:block}
.thumb .play{position:absolute;inset:0;display:flex;align-items:center;justify-content:center}
.thumb .play span{width:46px;height:46px;border-radius:50%;background:rgba(0,0,0,.55);color:#fff;
                  display:flex;align-items:center;justify-content:center;font-size:18px}
.thumb .cat{position:absolute;top:8px;left:8px;font-size:10px;font-weight:700;padding:3px 8px;
            border-radius:6px;color:#fff;letter-spacing:.03em}
.thumb .roas{position:absolute;top:8px;right:8px;font-size:12px;font-weight:800;padding:3px 8px;
             border-radius:6px;background:rgba(255,255,255,.94)}
.card .body{padding:11px 12px}
.card .nm{font-size:12px;font-weight:600;line-height:1.35;color:#3a2d1f;
          display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;min-height:33px}
.card .met{display:flex;gap:10px;margin-top:8px;font-size:11px;color:#8a7d6b;flex-wrap:wrap}
.card .met b{color:#3a2d1f}
.win{display:flex;gap:6px;margin-top:8px}
.win div{flex:1;text-align:center;background:#f7f2e8;border-radius:6px;padding:4px 2px}
.win .l{font-size:9px;color:#9a8d7b;font-weight:600}
.win .n{font-size:12px;font-weight:700;color:#3a2d1f}
.roasgood{color:#0a7d3c}.roasmid{color:#b07a00}.roasbad{color:#c0392b}
.empty{text-align:center;color:#9a8d7b;padding:40px;font-size:14px}
.count{font-size:12px;color:#8a7d6b;margin-bottom:10px}
.modal{position:fixed;inset:0;background:rgba(30,22,14,.55);display:none;align-items:center;
       justify-content:center;padding:20px;z-index:50}
.modal.on{display:flex}
.sheet{background:#fff;border-radius:14px;max-width:640px;width:100%;max-height:88vh;overflow:auto;padding:20px 22px}
.sheet h3{margin:0 0 3px;font-size:16px;color:#3a2d1f}
.sheet .meta{font-size:12px;color:#8a7d6b;margin-bottom:14px}
.sheet table{width:100%;border-collapse:collapse;font-size:13px}
.sheet th{text-align:left;color:#9a8d7b;font-size:11px;text-transform:uppercase;padding:6px 8px;
          border-bottom:1px solid #eee6d8}
.sheet td{padding:8px;border-bottom:1px solid #f4efe4}
.sheet .close{float:right;cursor:pointer;color:#9a8d7b;font-size:22px;line-height:1}
.watch{display:inline-block;margin-top:12px;background:#1877f2;color:#fff;text-decoration:none;
       padding:9px 16px;border-radius:8px;font-size:13px;font-weight:600}
.foot{font-size:11px;color:#9a8d7b;text-align:center;margin-top:24px;line-height:1.7}
@media(max-width:560px){.grid{grid-template-columns:repeat(auto-fill,minmax(46%,1fr))}}
"""

CAT_COLOR = {'Skin': '#c2410c', 'Hair': '#0d9488', 'Jewellery': '#a16207',
             'Crystals': '#7c3aed', 'Crystal Home Decor': '#be123c',
             'Offer': '#0a7d3c', 'Others': '#64748b'}

JS = """
const P = __PAYLOAD__;
const CC = __CATCOLOR__;
let f = {cat:'', q:'', roas:0, sort:'spend'};
const rupee = n => '₹' + Math.round(n).toLocaleString('en-IN');
const rclass = r => r>=2 ? 'roasgood' : r>=1.2 ? 'roasmid' : 'roasbad';

function summary(){
  const t = P.totals;
  document.getElementById('blocks').innerHTML =
    block('Videos', t.videos.toLocaleString('en-IN'), P.since+' → '+(P.until||'today'))
   +block('Spend', rupee(t.spend), '')
   +block('Revenue', rupee(t.revenue), '')
   +block('Blended ROAS', t.roas.toFixed(2), '')
   +block('With preview', t.previews+' / '+t.videos, 'click to watch');
  let chips = catchip('All', P.videos.length, '', true);
  const order=['Skin','Hair','Jewellery','Crystals','Crystal Home Decor','Offer','Others'];
  order.forEach(c=>{ if(P.categories[c]) chips += catchip(c, P.categories[c].videos, c); });
  document.getElementById('cats').innerHTML = chips;
}
const block=(k,v,s)=>`<div class="block"><div class="k">${k}</div><div class="v">${v}</div><div class="s">${s}</div></div>`;
const catchip=(label,n,c,all)=>`<div class="catchip ${((all&&!f.cat)||f.cat===c)?'on':''}" onclick="setCat('${c}')">
  <span style="width:9px;height:9px;border-radius:50%;background:${all?'#b08d57':CC[c]}"></span>${label}<span class="n">${n}</span></div>`;
function setCat(c){ f.cat = (f.cat===c?'':c); summary(); render(); }

function render(){
  let vids = P.videos.filter(v=>{
    if(f.cat && v.category!==f.cat) return false;
    if(f.roas && v.roas < f.roas) return false;
    if(f.q){ const q=f.q.toLowerCase(); if(!(v.name||'').toLowerCase().includes(q)) return false; }
    return true;
  });
  const s=f.sort;
  vids.sort((a,b)=> s==='roas'? b.roas-a.roas : s==='tries'? b.tries-a.tries
                    : s==='survival'? (b.deployments[0]?.survived||0)-(a.deployments[0]?.survived||0)
                    : b.spend-a.spend);
  document.getElementById('count').textContent = vids.length+' videos'
     + (f.cat?' · '+f.cat:'') + (f.roas?' · ROAS ≥ '+f.roas:'');
  if(!vids.length){ document.getElementById('grid').innerHTML='<div class="empty">No videos match.</div>'; return; }
  document.getElementById('grid').innerHTML = vids.map((v,i)=>card(v,P.videos.indexOf(v))).join('');
}
function card(v,idx){
  const thumb = v.thumb ? `style="background-image:url('${v.thumb}')"` : '';
  const win = (l,r)=>`<div><div class="l">${l}</div><div class="n ${rclass(r)}">${r?r.toFixed(2):'—'}</div></div>`;
  return `<div class="card" onclick="detail(${idx})">
    <div class="thumb" ${thumb}>
      <div class="cat" style="background:${CC[v.category]||'#64748b'}">${v.category}</div>
      <div class="roas ${rclass(v.roas)}">${v.roas.toFixed(2)}</div>
      <div class="play"><span>▶</span></div>
    </div>
    <div class="body">
      <div class="nm">${esc(v.name)}</div>
      <div class="met"><span><b>${rupee(v.spend)}</b> spend</span>
        <span><b>${v.tries}</b> ${v.tries>1?'tries':'try'}</span>
        <span><b>${v.deployments[0]?.survived??'—'}</b>d</span></div>
      <div class="win">${win('1d',v.roas_1d)}${win('3d',v.roas_3d)}${win('7d',v.roas_7d)}</div>
    </div></div>`;
}
function detail(idx){
  const v=P.videos[idx];
  const rows=v.deployments.map(d=>`<tr><td>${esc(d.campaign||'')}</td><td>${d.account||''}</td>
    <td>${d.survived??'—'}d</td><td>${d.status||''}</td><td>${rupee(d.spend)}</td>
    <td class="${rclass(d.roas)}">${d.roas.toFixed(2)}</td></tr>`).join('');
  document.getElementById('sheet').innerHTML =
    `<span class="close" onclick="closeM()">×</span>
     <h3>${esc(v.name)}</h3>
     <div class="meta">${v.category} · ${v.tries} campaign${v.tries>1?'s':''} · launched ${v.launch}
       · ${rupee(v.spend)} spend · ${rupee(v.revenue)} revenue · ROAS ${v.roas.toFixed(2)}
       · 1d ${v.roas_1d.toFixed(2)} / 3d ${v.roas_3d.toFixed(2)} / 7d ${v.roas_7d.toFixed(2)}</div>
     ${v.permalink?`<a class="watch" href="${v.permalink}" target="_blank" rel="noopener">▶ Watch video</a>`:'<span style="color:#c0392b;font-size:12px">no preview link</span>'}
     <h4 style="margin:18px 0 4px;font-size:13px;color:#8a7d6b">Deployments (${v.deployments.length})</h4>
     <table><tr><th>Campaign</th><th>Account</th><th>Survived</th><th>Status</th><th>Spend</th><th>ROAS</th></tr>${rows}</table>`;
  document.getElementById('modal').classList.add('on');
}
function closeM(){ document.getElementById('modal').classList.remove('on'); }
function esc(s){ return (s||'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c])); }

document.getElementById('q').oninput = e=>{ f.q=e.target.value; render(); };
document.getElementById('roas').onchange = e=>{ f.roas=parseFloat(e.target.value)||0; summary(); render(); };
document.getElementById('sort').onchange = e=>{ f.sort=e.target.value; render(); };
document.getElementById('modal').onclick = e=>{ if(e.target.id==='modal') closeM(); };
summary(); render();
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--data', default='antariksh/paras_videos.json')
    ap.add_argument('--out', default='antariksh/paras-videos.html')
    args = ap.parse_args()

    payload = json.loads(Path(args.data).read_text())
    js = (JS.replace('__PAYLOAD__', json.dumps(payload, default=str))
            .replace('__CATCOLOR__', json.dumps(CAT_COLOR)))
    html = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>Paras Video Report</title><style>{CSS}</style></head><body><div class="wrap">
<h1>Paras Video Report</h1>
<div class="sub">Every Paras video creative on Meta ads · {payload['since']} → {payload['until'] or 'today'} · click a video to watch and see its campaign breakdown</div>
<div class="blocks" id="blocks"></div>
<div class="cats" id="cats"></div>
<div class="bar">
  <input id="q" placeholder="Search product / video name…">
  <label>ROAS ≥ <select id="roas"><option value="0">any</option><option>1</option><option>1.5</option><option>2</option><option>3</option></select></label>
  <label>Sort <select id="sort"><option value="spend">Spend</option><option value="roas">ROAS</option><option value="tries">Tries</option><option value="survival">Survival</option></select></label>
</div>
<div class="count" id="count"></div>
<div class="grid" id="grid"></div>
<div class="modal" id="modal"><div class="sheet" id="sheet"></div></div>
<div class="foot">ROAS 1d/3d/7d = first N days since the video launched · Tries = distinct campaigns it delivered in · Meta pixel-attributed</div>
</div><script>{js}</script></body></html>"""
    Path(args.out).write_text(html, encoding='utf-8')
    print(f"wrote {args.out} ({len(html) // 1024} KB, {payload['totals']['videos']} videos)")


if __name__ == '__main__':
    main()
