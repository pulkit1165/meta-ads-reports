#!/usr/bin/env python3
"""
Antariksh dashboard generator.

Renders the Antariksh home page (single static HTML, all interaction
client-side) from state/ntn.db. Mirrors the proven v2 pattern: embed a
trimmed per-ad-day payload + Shopify ground-truth daily rows, then do all
filtering / date-range / aggregation in the browser (<100ms, zero API calls).

Home page blocks (per operator spec):
  1. Hero KPIs   - live Sales / Orders / ROAS, with SM/SML/NBP filter.
                   Sales+orders = Shopify ground truth; ROAS = sales / Meta spend.
  2. Fulfillment - Prepaid% (real, from financial_status) + Delivered%
                   (placeholder until a courier feed is ingested), all-portal
                   and website-wise split.
  3. Categories  - per-category spend + orders + avg ROAS, driven by a calendar
                   date range.
  4. Creative    - split by Paras/Motion/Static/Partnership/AI/Wanda/Other:
                   count, spend, avg ROAS.

The hero reads a live snapshot from a Worker/KV endpoint when configured
(window.ANTARIKSH_LIVE_URL); otherwise it falls back to the embedded latest
day and the freshness pill reflects the embedded data age.

Usage:
  python3 scripts/v2/build_antariksh.py
  python3 scripts/v2/build_antariksh.py --db /tmp/ntn.db --days 90
  python3 scripts/v2/build_antariksh.py --out antariksh/index.html
"""

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _utils import db_connect, IST, DEFAULT_DB  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_OUT = REPO_ROOT / 'antariksh' / 'index.html'


def fetch_ad_days(conn, since):
    rows = conn.execute('''
        SELECT d.date, d.portal,
               COALESCE(NULLIF(TRIM(m.category),''),'Other')      AS cat,
               COALESCE(NULLIF(TRIM(m.creative_type),''),'Other') AS ct,
               d.ad_id,
               ROUND(COALESCE(d.spend,0),2),
               COALESCE(d.purchases,0),
               ROUND(COALESCE(d.revenue,0),2)
        FROM meta_ads_daily d
        LEFT JOIN meta_ads_meta m ON d.ad_id = m.ad_id
        WHERE d.spend > 0 AND d.date >= ?
        ORDER BY d.date
    ''', (since,)).fetchall()
    # short keys to keep payload lean
    return [{'d': r[0], 'p': r[1], 'c': r[2], 'ct': r[3], 'id': r[4],
             's': r[5], 'pu': r[6], 'rev': r[7]} for r in rows]


def fetch_shop_days(conn, since):
    rows = conn.execute('''
        SELECT date, portal, orders, revenue, prepaid_orders, cod_orders
        FROM antariksh_shopify_daily
        WHERE date >= ?
        ORDER BY date
    ''', (since,)).fetchall()
    return [{'d': r[0], 'p': r[1], 'o': r[2], 'rev': r[3],
             'pp': r[4], 'cod': r[5]} for r in rows]


def build_payload(conn, days):
    since = (datetime.now(IST).date() - timedelta(days=days - 1)).isoformat()
    ad_days = fetch_ad_days(conn, since)
    shop = fetch_shop_days(conn, since)
    dates = [r['d'] for r in shop] + [r['d'] for r in ad_days]
    portals = sorted({r['p'] for r in shop} | {r['p'] for r in ad_days})
    return {
        'generated_at': datetime.now(IST).isoformat(),
        'today': datetime.now(IST).date().isoformat(),
        'minDate': min(dates) if dates else since,
        'maxDate': max(dates) if dates else since,
        'portals': portals,
        'adDays': ad_days,
        'shop': shop,
    }


HD_PRESETS = ['today', 'yesterday', 'last_7d', 'last_30d']


def build_homedecor(presets=HD_PRESETS):
    """Rich Home-Decor payload per preset, fetched live from Meta at build time
    (campaigns -> ad sets -> creatives, product + website rollups, creative
    report). Embedded into the page so the browser makes zero API calls, exactly
    like the main payload. Degrades to {} when META_ACCESS_TOKEN is absent or a
    preset errors, so it never breaks the main build."""
    import os
    out = {}
    if not os.getenv('META_ACCESS_TOKEN'):
        print("  home-decor: META_ACCESS_TOKEN not set — embedding empty payload",
              file=sys.stderr)
        return out
    try:
        import home_decor_dashboard_data as hdd
    except Exception as e:  # noqa: BLE001
        print(f"  home-decor: import failed ({e}) — embedding empty", file=sys.stderr)
        return out
    for p in presets:
        try:
            out[p] = hdd.build(p)
            ov = out[p].get('overview', {})
            print(f"  home-decor[{p}]: {ov.get('campaigns', 0)} camps, "
                  f"Rs {ov.get('budget', 0):,}/day", file=sys.stderr)
        except Exception as e:  # noqa: BLE001
            print(f"  home-decor[{p}] failed: {e}", file=sys.stderr)
    return out


def render(payload, homedecor=None):
    data = json.dumps(payload, separators=(',', ':'))
    hd = json.dumps(homedecor or {}, separators=(',', ':'), ensure_ascii=False)
    return (HTML.replace('/*__PAYLOAD__*/', data)
                .replace('/*__HOMEDECOR__*/', hd))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', default=str(DEFAULT_DB))
    ap.add_argument('--days', type=int, default=90)
    ap.add_argument('--out', default=str(DEFAULT_OUT))
    args = ap.parse_args()

    conn = db_connect(Path(args.db))
    payload = build_payload(conn, args.days)
    conn.close()

    homedecor = build_homedecor()

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(payload, homedecor))
    kb = out.stat().st_size / 1024
    print(f"wrote {out}  ({kb:.0f} KB)  "
          f"{len(payload['adDays'])} ad-days, {len(payload['shop'])} shop-days, "
          f"span {payload['minDate']}..{payload['maxDate']}")


HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Antariksh - NTN Ads Reports</title>
<style>
  :root{
    --bg:#f5f6f8;--panel:#fff;--ink:#1c2330;--muted:#6b7686;--line:#e6e9ef;
    --brand:#2f5bff;--brand-soft:#eaf0ff;--good:#0c9b5b;--bad:#d23b3b;--warn:#c98a00;
    --shadow:0 1px 2px rgba(16,24,40,.05),0 1px 3px rgba(16,24,40,.08);
  }
  *{box-sizing:border-box}
  html,body{margin:0;height:100%}
  body{font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,Roboto,Arial,sans-serif;color:var(--ink);background:var(--bg)}
  .app{display:grid;grid-template-columns:240px 1fr;min-height:100vh}
  .side{background:#0f1830;color:#cdd5e3;position:sticky;top:0;height:100vh;display:flex;flex-direction:column}
  .brand{padding:18px 18px 14px;display:flex;align-items:center;gap:10px;border-bottom:1px solid rgba(255,255,255,.08)}
  .brand .logo{width:30px;height:30px;border-radius:8px;background:linear-gradient(135deg,#3b6bff,#6ad);display:grid;place-items:center;font-weight:700;color:#fff}
  .brand b{font-size:15px;color:#fff}.brand small{display:block;color:#8693ad;font-size:11px}
  .nav{padding:10px;flex:1;overflow:auto}
  .grp{margin:14px 8px 6px;font-size:10.5px;letter-spacing:.09em;text-transform:uppercase;color:#67738f}
  .item{display:flex;gap:10px;padding:8px 10px;border-radius:8px;cursor:pointer;color:#c4ccdb;font-size:13.5px;margin:1px 0}
  .item .ic{width:16px;text-align:center;opacity:.85}
  .item:hover{background:rgba(255,255,255,.06)}.item.active{background:var(--brand);color:#fff;font-weight:600}
  .side-foot{padding:12px 16px;border-top:1px solid rgba(255,255,255,.08);font-size:11px;color:#7d89a3}

  .main{min-width:0;display:flex;flex-direction:column}
  .topbar{position:sticky;top:0;z-index:5;background:rgba(245,246,248,.9);backdrop-filter:blur(8px);border-bottom:1px solid var(--line);padding:11px 22px;display:flex;align-items:center;gap:10px;flex-wrap:wrap}
  .crumb{font-weight:700;font-size:16px}.crumb small{display:block;font-weight:500;color:var(--muted);font-size:12px;margin-top:1px}
  .spacer{flex:1}
  .seg{display:flex;background:var(--panel);border:1px solid var(--line);border-radius:9px;overflow:hidden;box-shadow:var(--shadow)}
  .seg button{border:0;background:transparent;padding:7px 11px;font-size:12.5px;cursor:pointer;color:var(--muted);font-weight:600}
  .seg button.on{background:var(--brand);color:#fff}
  .pill{display:inline-flex;align-items:center;gap:7px;background:var(--panel);border:1px solid var(--line);border-radius:999px;padding:6px 12px;font-size:12px;box-shadow:var(--shadow)}
  .pill .led{width:8px;height:8px;border-radius:50%;background:var(--good)}
  .pill.amber .led{background:var(--warn)}.pill.red .led{background:var(--bad)}
  .pill.range{color:var(--muted);font-weight:600}

  .wrap{padding:22px;max-width:1200px;width:100%}
  .section{margin-bottom:26px;scroll-margin-top:80px}
  .section>h2{font-size:13px;letter-spacing:.05em;text-transform:uppercase;color:var(--muted);margin:0 0 12px}

  .kpis{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}
  .kpi{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:18px;box-shadow:var(--shadow)}
  .kpi .lbl{font-size:12px;color:var(--muted);font-weight:600;display:flex;align-items:center;gap:6px}
  .kpi .val{font-size:30px;font-weight:760;margin:8px 0 2px;letter-spacing:-.01em}
  .kpi .sub{font-size:12px;color:var(--muted)}
  .tag{font-size:10px;font-weight:700;padding:1px 6px;border-radius:6px;background:var(--brand-soft);color:var(--brand)}
  .tag.gt{background:#e7f7ee;color:var(--good)}.tag.px{background:#fdeaea;color:var(--bad)}

  .grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px}
  .grid4{display:grid;grid-template-columns:repeat(4,1fr);gap:14px}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:16px 18px;box-shadow:var(--shadow)}
  .card h3{margin:0 0 2px;font-size:14.5px}.card .csub{color:var(--muted);font-size:12px;margin-bottom:12px}
  .ph{position:relative}
  .ph::after{content:"data source pending";position:absolute;top:14px;right:16px;font-size:10px;font-weight:700;color:var(--warn);background:#fff7e6;border:1px solid #ffe3a3;padding:2px 7px;border-radius:6px}
  .big{font-size:26px;font-weight:750}
  .muted{color:var(--muted)}

  table{width:100%;border-collapse:collapse;font-size:13px}
  th,td{padding:9px 10px;text-align:right;border-bottom:1px solid var(--line);white-space:nowrap}
  th:first-child,td:first-child{text-align:left}
  thead th{font-size:11px;letter-spacing:.04em;text-transform:uppercase;color:var(--muted);font-weight:700;cursor:pointer;user-select:none}
  tbody tr:hover{background:#fafbff}
  .chip{display:inline-block;padding:2px 9px;border-radius:999px;font-size:11.5px;background:#eef1f6;color:#42506b;font-weight:600}
  .rg{font-weight:700}.rg.g{color:var(--good)}.rg.m{color:var(--warn)}.rg.b{color:var(--bad)}

  .cal{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
  .cal input[type=date]{border:1px solid var(--line);border-radius:8px;padding:6px 9px;font:inherit;background:var(--panel)}
  .note{background:#fff7e6;border:1px solid #ffe3a3;color:#7a5500;border-radius:10px;padding:9px 13px;font-size:12.5px;margin-bottom:16px}
  @media(max-width:980px){.app{grid-template-columns:1fr}.side{display:none}.kpis{grid-template-columns:1fr}.grid2,.grid4{grid-template-columns:1fr}}
</style>
</head>
<body>
<div class="app">
  <aside class="side">
    <div class="brand"><div class="logo">A</div><div><b>Antariksh</b><small>NTN Ads - reports</small></div></div>
    <nav class="nav">
      <div class="grp">Home</div>
      <div class="item active" data-go="sec-hero"><span class="ic">&#9670;</span> Live KPIs</div>
      <div class="item" data-go="sec-fulfil"><span class="ic">&#128666;</span> Fulfillment</div>
      <div class="item" data-go="sec-cat"><span class="ic">&#9638;</span> Categories</div>
      <div class="item" data-go="sec-creative"><span class="ic">&#10022;</span> Creative split</div>
      <div class="grp">By category</div>
      <div class="item" data-cat="Skin"><span class="ic">&#129496;</span> Skin care</div>
      <div class="item" data-cat="Hair"><span class="ic">&#128088;</span> Hair</div>
      <div class="item" data-cat="24K Jewellery"><span class="ic">&#128142;</span> 24K Jewellery</div>
      <div class="item" data-cat="Crystal Home Decor"><span class="ic">&#128302;</span> Crystal Home Decor</div>
      <div class="item" data-cat="Aibot"><span class="ic">&#129302;</span> AI bot</div>
    </nav>
    <div class="side-foot" id="foot">loading...</div>
  </aside>

  <div class="main">
    <div class="topbar">
      <div class="crumb">Home<small>Live sales, ROAS &amp; breakdowns</small></div>
      <div class="spacer"></div>
      <div class="seg" id="portalseg"></div>
      <div class="pill range" id="rangepill">Range: -</div>
      <div class="pill" id="freshpill"><span class="led"></span> <span id="freshtxt">-</span></div>
    </div>

    <div class="wrap">
      <div class="note" id="stalenote" style="display:none"></div>

      <div id="view-home">
      <!-- HERO -->
      <section class="section" id="sec-hero">
        <h2>Live KPIs <span id="heroscope" class="muted" style="text-transform:none;letter-spacing:0"></span></h2>
        <div class="kpis">
          <div class="kpi"><div class="lbl">Sales <span class="tag gt">SHOPIFY</span></div><div class="val" id="kSales">-</div><div class="sub">real orders, refreshes ~5 min</div></div>
          <div class="kpi"><div class="lbl">Orders <span class="tag gt">SHOPIFY</span></div><div class="val" id="kOrders">-</div><div class="sub">non-cancelled</div></div>
          <div class="kpi"><div class="lbl">ROAS <span class="tag">SHOPIFY / META</span></div><div class="val" id="kRoas">-</div><div class="sub">sales / Meta spend (all accounts), hourly</div></div>
        </div>
      </section>

      <!-- FULFILLMENT -->
      <section class="section" id="sec-fulfil">
        <h2>Fulfillment</h2>
        <div class="grid2">
          <div class="card">
            <h3>Prepaid %</h3><div class="csub">share of orders paid online (vs COD) - all portals + split</div>
            <div id="prepaidWrap"></div>
          </div>
          <div class="card ph">
            <h3>Delivered %</h3><div class="csub">all portals + website-wise split</div>
            <div id="deliveredWrap" style="opacity:.45">
              <div class="big">--%</div>
              <div class="muted" style="font-size:12.5px;margin-top:6px">Needs a courier / fulfillment feed (Shiprocket / Delhivery). Block reserved; wire data source later.</div>
            </div>
          </div>
        </div>
      </section>

      <!-- CATEGORIES -->
      <section class="section" id="sec-cat">
        <h2>Categories</h2>
        <div class="card">
          <div style="display:flex;align-items:center;gap:14px;flex-wrap:wrap;margin-bottom:12px">
            <div><h3 style="margin:0">Spend, orders &amp; ROAS by category</h3>
              <div class="csub" style="margin:0">spend = real Meta &middot; orders/ROAS = Meta-attributed (pixel) &middot; <span id="catShopLine"></span></div></div>
            <div class="spacer"></div>
            <div class="cal">
              <span class="muted">From</span><input type="date" id="dFrom">
              <span class="muted">To</span><input type="date" id="dTo">
            </div>
          </div>
          <table id="catTable">
            <thead><tr>
              <th data-k="cat">Category</th><th data-k="spend">Spend</th>
              <th data-k="orders">Orders <span class="tag px">PX</span></th>
              <th data-k="ads">Ads</th><th data-k="roas">Avg ROAS</th>
            </tr></thead>
            <tbody></tbody>
          </table>
        </div>
      </section>

      <!-- CREATIVE -->
      <section class="section" id="sec-creative">
        <h2>Creative split</h2>
        <div class="card">
          <h3>By creative type</h3>
          <div class="csub">count of distinct ads, spend &amp; avg ROAS in the selected date range (uses the calendar above)</div>
          <div class="grid4" id="creativeCards" style="margin-bottom:14px"></div>
          <table id="creativeTable">
            <thead><tr>
              <th data-k="ct">Creative</th><th data-k="ads">Ads</th>
              <th data-k="spend">Spend</th><th data-k="share">Spend %</th>
              <th data-k="roas">Avg ROAS</th>
            </tr></thead>
            <tbody></tbody>
          </table>
        </div>
      </section>
      </div><!-- /view-home -->

      <!-- CATEGORY DETAIL -->
      <div id="view-category" style="display:none">
        <section class="section">
          <h2 id="catTitle">Category <span id="catScope" class="muted" style="text-transform:none;letter-spacing:0"></span></h2>
          <div class="kpis" style="grid-template-columns:repeat(4,1fr)">
            <div class="kpi"><div class="lbl">Spend <span class="tag">META</span></div><div class="val" id="cSpend">-</div><div class="sub">real ad spend, all accounts</div></div>
            <div class="kpi"><div class="lbl">Orders <span class="tag px">PIXEL</span></div><div class="val" id="cOrders">-</div><div class="sub">Meta-attributed purchases</div></div>
            <div class="kpi"><div class="lbl">Revenue <span class="tag px">PIXEL</span></div><div class="val" id="cRev">-</div><div class="sub">Meta-attributed value</div></div>
            <div class="kpi"><div class="lbl">Avg ROAS <span class="tag px">PIXEL</span></div><div class="val" id="cRoas">-</div><div class="sub">pixel revenue / spend</div></div>
          </div>
        </section>
        <section class="section">
          <div class="card">
            <div style="display:flex;align-items:center;gap:14px;flex-wrap:wrap;margin-bottom:12px">
              <div><h3 style="margin:0">Creative split within <span id="catTitle2"></span></h3>
                <div class="csub" style="margin:0">spend = real Meta &middot; orders/ROAS = Meta-attributed (pixel)</div></div>
              <div class="spacer"></div>
              <div class="cal">
                <span class="muted">From</span><input type="date" id="cdFrom">
                <span class="muted">To</span><input type="date" id="cdTo">
              </div>
            </div>
            <table id="catCreativeTable">
              <thead><tr>
                <th data-k="ct">Creative</th><th data-k="ads">Ads</th>
                <th data-k="spend">Spend</th>
                <th data-k="orders">Orders <span class="tag px">PX</span></th>
                <th data-k="share">Spend %</th><th data-k="roas">Avg ROAS</th>
              </tr></thead>
              <tbody></tbody>
            </table>
          </div>
        </section>
      </div><!-- /view-category -->

      <!-- HOME DECOR (rich module: live from Meta, embedded at build) -->
      <div id="view-homedecor" style="display:none">
        <section class="section">
          <h2 style="margin-bottom:6px">&#128302; Crystal Home Decor <span class="muted" id="hdScope" style="text-transform:none;letter-spacing:0;font-size:13px"></span></h2>
          <div style="display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin-bottom:6px">
            <div class="seg" id="hdTabs"></div>
            <div class="spacer"></div>
            <span class="muted" style="font-size:12px">Range</span>
            <div class="seg" id="hdPresetSeg"></div>
          </div>
          <div class="muted" id="hdFetched" style="font-size:12px;margin-bottom:8px"></div>
        </section>
        <div id="hdBody"></div>
      </div><!-- /view-homedecor -->
    </div>
  </div>
</div>

<script>
const PAYLOAD = /*__PAYLOAD__*/;
const HOMEDECOR = /*__HOMEDECOR__*/;
const fmtINR = n => { n=Math.round(n||0);
  if(Math.abs(n)>=1e7) return '₹'+(n/1e7).toFixed(2)+'Cr';
  if(Math.abs(n)>=1e5) return '₹'+(n/1e5).toFixed(2)+'L';
  if(Math.abs(n)>=1e3) return '₹'+(n/1e3).toFixed(1)+'k';
  return '₹'+n; };
const fmtNum = n => (n||0).toLocaleString('en-IN');
const roasClass = r => r>=2 ? 'g' : r>=1.2 ? 'm' : 'b';

let STATE = { portal:'ALL', from:PAYLOAD.minDate, to:PAYLOAD.maxDate, live:null, view:'home', cat:null };

// ---------- filters ----------
const inPortal = p => STATE.portal==='ALL' || p===STATE.portal;
const inRange  = d => d>=STATE.from && d<=STATE.to;
const adRows   = () => PAYLOAD.adDays.filter(r=>inPortal(r.p)&&inRange(r.d));
const shopRows = () => PAYLOAD.shop.filter(r=>inPortal(r.p)&&inRange(r.d));

// ---------- hero ----------
function renderHero(){
  let sales=0, orders=0, spend=0;
  if(STATE.live){
    const L = STATE.live[STATE.portal] || STATE.live.ALL || {};
    sales=L.sales||0; orders=L.orders||0; spend=L.spend||0;
  } else {
    // no live feed wired yet: show the latest available day (= today once
    // the hourly ingest has run) so the hero still reflects "today so far".
    const last = PAYLOAD.maxDate;
    PAYLOAD.shop.filter(r=>inPortal(r.p)&&r.d===last).forEach(r=>{sales+=r.rev;orders+=r.o;});
    PAYLOAD.adDays.filter(r=>inPortal(r.p)&&r.d===last).forEach(r=>{spend+=r.s;});
    const isToday = last===PAYLOAD.today;
    document.getElementById('heroscope').textContent =
      isToday ? ' - today '+last+' (so far)' : ' - latest day '+last;
  }
  const roas = spend>0 ? sales/spend : 0;
  document.getElementById('kSales').textContent = fmtINR(sales);
  document.getElementById('kOrders').textContent = fmtNum(orders);
  document.getElementById('kRoas').innerHTML = '<span class="rg '+roasClass(roas)+'">'+roas.toFixed(2)+'×</span>';
}

// ---------- prepaid ----------
function renderPrepaid(){
  const byP = {}; let tO=0,tP=0;
  PAYLOAD.shop.filter(r=>inRange(r.d)).forEach(r=>{
    byP[r.p]=byP[r.p]||{o:0,pp:0}; byP[r.p].o+=r.o; byP[r.p].pp+=r.pp; tO+=r.o; tP+=r.pp;
  });
  const pct=(p,o)=>o>0?(100*p/o).toFixed(1)+'%':'-';
  let h='<div class="big">'+pct(tP,tO)+'</div><div class="muted" style="font-size:12px;margin-bottom:10px">all portals prepaid</div>';
  h+='<table><thead><tr><th>Portal</th><th>Orders</th><th>Prepaid</th><th>COD</th></tr></thead><tbody>';
  Object.keys(byP).sort().forEach(p=>{const b=byP[p];
    h+='<tr><td><span class="chip">'+p+'</span></td><td>'+fmtNum(b.o)+'</td><td>'+pct(b.pp,b.o)+'</td><td>'+pct(b.o-b.pp,b.o)+'</td></tr>';});
  h+='</tbody></table>';
  document.getElementById('prepaidWrap').innerHTML=h;
}

// ---------- categories ----------
let catSort={k:'spend',dir:-1};
function renderCat(){
  const m={};
  adRows().forEach(r=>{const a=m[r.c]||(m[r.c]={spend:0,orders:0,rev:0,ads:new Set()});
    a.spend+=r.s; a.orders+=r.pu; a.rev+=r.rev; a.ads.add(r.id);});
  let rows=Object.keys(m).map(c=>{const a=m[c];return{cat:c,spend:a.spend,orders:a.orders,ads:a.ads.size,roas:a.spend>0?a.rev/a.spend:0};});
  rows.sort((x,y)=>{const k=catSort.k; const va=x[k],vb=y[k];
    return (typeof va==='string'?va.localeCompare(vb):va-vb)*catSort.dir;});
  let sg=shopRows().reduce((s,r)=>{s.o+=r.o;s.rev+=r.rev;return s;},{o:0,rev:0});
  document.getElementById('catShopLine').innerHTML='Shopify total this range: <b>'+fmtINR(sg.rev)+'</b> / <b>'+fmtNum(sg.o)+'</b> orders';
  let h='';
  rows.forEach(r=>{h+='<tr><td><span class="chip">'+r.cat+'</span></td><td>'+fmtINR(r.spend)+'</td><td>'+fmtNum(Math.round(r.orders))+'</td><td>'+fmtNum(r.ads)+'</td><td><span class="rg '+roasClass(r.roas)+'">'+r.roas.toFixed(2)+'×</span></td></tr>';});
  document.querySelector('#catTable tbody').innerHTML=h;
}

// ---------- creative ----------
let ctSort={k:'spend',dir:-1};
function renderCreative(){
  const m={}; let totSpend=0;
  adRows().forEach(r=>{const a=m[r.ct]||(m[r.ct]={spend:0,rev:0,ads:new Set()});
    a.spend+=r.s; a.rev+=r.rev; a.ads.add(r.id); totSpend+=r.s;});
  let rows=Object.keys(m).map(ct=>{const a=m[ct];return{ct:ct,ads:a.ads.size,spend:a.spend,share:totSpend>0?100*a.spend/totSpend:0,roas:a.spend>0?a.rev/a.spend:0};});
  rows.sort((x,y)=>{const k=ctSort.k;const va=x[k],vb=y[k];return (typeof va==='string'?va.localeCompare(vb):va-vb)*ctSort.dir;});
  // cards (top 4 by spend)
  let cards=rows.slice().sort((a,b)=>b.spend-a.spend).slice(0,4).map(r=>
    '<div class="card" style="box-shadow:none;border-radius:10px"><div class="lbl muted" style="font-size:12px;font-weight:600">'+r.ct+'</div><div style="font-size:20px;font-weight:750;margin:4px 0">'+fmtNum(r.ads)+' ads</div><div style="font-size:12px" class="muted">'+fmtINR(r.spend)+' &middot; <span class="rg '+roasClass(r.roas)+'">'+r.roas.toFixed(2)+'×</span></div></div>').join('');
  document.getElementById('creativeCards').innerHTML=cards;
  let h='';
  rows.forEach(r=>{h+='<tr><td><span class="chip">'+r.ct+'</span></td><td>'+fmtNum(r.ads)+'</td><td>'+fmtINR(r.spend)+'</td><td>'+r.share.toFixed(1)+'%</td><td><span class="rg '+roasClass(r.roas)+'">'+r.roas.toFixed(2)+'×</span></td></tr>';});
  document.querySelector('#creativeTable tbody').innerHTML=h;
}

// ---------- chrome ----------
function renderChrome(){
  document.getElementById('rangepill').textContent='Range: '+STATE.from+' → '+STATE.to;
  const gen=new Date(PAYLOAD.generated_at), maxD=PAYLOAD.maxDate;
  const ageDays=Math.floor((Date.now()-new Date(maxD+'T23:59:59+05:30'))/864e5);
  const fp=document.getElementById('freshpill');
  fp.className='pill'+(ageDays>=2?' red':ageDays>=1?' amber':'');
  document.getElementById('freshtxt').textContent = ageDays<=0?'data current ('+maxD+')':'data '+ageDays+'d old (latest '+maxD+')';
  if(ageDays>=2){const n=document.getElementById('stalenote');n.style.display='';
    n.innerHTML='⚠️ Showing embedded history (latest <b>'+maxD+'</b>, '+ageDays+' days old). The live 5-min / hourly feed is not connected yet, so the hero shows the latest available day. Numbers below are real, just not today.';}
}

// ---------- category detail ----------
const CAT_LABELS={'Skin':'Skin care','Hair':'Hair','24K Jewellery':'24K Jewellery','Crystal Home Decor':'Crystal Home Decor','Aibot':'AI bot'};
let catCtSort={k:'spend',dir:-1};
function renderCategory(){
  const cat=STATE.cat; if(!cat)return;
  const label=CAT_LABELS[cat]||cat;
  const rows=adRows().filter(r=>r.c===cat);
  let spend=0,orders=0,rev=0; const ads=new Set();
  rows.forEach(r=>{spend+=r.s;orders+=r.pu;rev+=r.rev;ads.add(r.id);});
  const roas=spend>0?rev/spend:0;
  const scope=(STATE.portal==='ALL'?'all portals':STATE.portal)+' &middot; '+STATE.from+' → '+STATE.to;
  document.getElementById('catTitle').innerHTML=label+' <span class="muted" style="text-transform:none;letter-spacing:0;font-size:13px">'+scope+'</span>';
  document.getElementById('catTitle2').textContent=label;
  document.getElementById('cSpend').textContent=fmtINR(spend);
  document.getElementById('cOrders').textContent=fmtNum(Math.round(orders));
  document.getElementById('cRev').textContent=fmtINR(rev);
  document.getElementById('cRoas').innerHTML='<span class="rg '+roasClass(roas)+'">'+roas.toFixed(2)+'×</span>';
  const m={}; let tot=0;
  rows.forEach(r=>{const a=m[r.ct]||(m[r.ct]={spend:0,rev:0,orders:0,ads:new Set()});
    a.spend+=r.s;a.rev+=r.rev;a.orders+=r.pu;a.ads.add(r.id);tot+=r.s;});
  let cr=Object.keys(m).map(ct=>{const a=m[ct];return{ct:ct,ads:a.ads.size,spend:a.spend,orders:a.orders,share:tot>0?100*a.spend/tot:0,roas:a.spend>0?a.rev/a.spend:0};});
  cr.sort((x,y)=>{const k=catCtSort.k,va=x[k],vb=y[k];return (typeof va==='string'?va.localeCompare(vb):va-vb)*catCtSort.dir;});
  let h='';
  cr.forEach(r=>{h+='<tr><td><span class="chip">'+r.ct+'</span></td><td>'+fmtNum(r.ads)+'</td><td>'+fmtINR(r.spend)+'</td><td>'+fmtNum(Math.round(r.orders))+'</td><td>'+r.share.toFixed(1)+'%</td><td><span class="rg '+roasClass(r.roas)+'">'+r.roas.toFixed(2)+'×</span></td></tr>';});
  document.querySelector('#catCreativeTable tbody').innerHTML=h||'<tr><td colspan="6" class="muted">No spend in this range.</td></tr>';
}

// ===================== HOME DECOR MODULE (embedded Meta payload) =====================
let HDPRESET='yesterday', HDTAB='overview';
const HD_PRESET_LABELS={today:'Today',yesterday:'Yesterday',last_7d:'Last 7D',last_30d:'Last 30D'};
const HD_TABS=[['overview','Category Overview'],['campaigns','Campaigns'],['budget','Product-Wise Budget'],['creative','Creative Report'],['clearance','Stock Clearance'],['profit','Product Profitability']];
function hdEsc(s){return (s==null?'':String(s)).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
function rg(r){r=r||0;return '<span class="rg '+roasClass(r)+'">'+r.toFixed(2)+'×</span>';}
function hdAgo(ts){let s=Math.floor(Date.now()/1000-ts);if(s<0)s=0;if(s<60)return s+'s ago';if(s<3600)return Math.floor(s/60)+'m ago';if(s<86400)return Math.floor(s/3600)+'h ago';return Math.floor(s/86400)+'d ago';}
function hdPick(){return (HOMEDECOR&&(HOMEDECOR[HDPRESET]||HOMEDECOR.yesterday||HOMEDECOR[Object.keys(HOMEDECOR)[0]]))||null;}
function hdKpi(l,v,sub,tag){return '<div class="kpi"><div class="lbl">'+l+(tag?' <span class="tag">'+tag+'</span>':'')+'</div><div class="val">'+v+'</div><div class="sub">'+(sub||'')+'</div></div>';}
function hdCard(ad){
  return '<div style="width:120px;font-size:10px" class="muted">'+
    '<div style="width:120px;height:120px;background:var(--bg2,#f0f2f7);border:1px solid var(--line,#e6e9f2);border-radius:8px;overflow:hidden;display:flex;align-items:center;justify-content:center">'+
    (ad.thumbnail?'<img src="'+ad.thumbnail+'" loading="lazy" referrerpolicy="no-referrer" style="max-width:100%;max-height:100%">':'<span>no preview</span>')+'</div>'+
    '<div style="margin-top:4px;display:flex;justify-content:space-between;align-items:center"><span class="chip">'+hdEsc(ad.type||'—')+'</span>'+rg(ad.roas)+'</div>'+
    '<div style="margin-top:3px;color:var(--ink,#1f2937);overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="'+hdEsc(ad.name)+'">'+hdEsc(ad.name)+'</div>'+
    '<div style="margin-top:2px">'+fmtINR(ad.spend)+' &middot; '+fmtNum(ad.purchases)+' ord</div></div>';
}
function hdFmtCard(l,o){o=o||{};return '<div class="card" style="box-shadow:none;border-radius:10px"><div class="lbl muted" style="font-size:12px;font-weight:600">'+l+'</div><div style="font-size:20px;font-weight:750;margin:4px 0">'+fmtNum(o.count||0)+' ads</div><div style="font-size:12px" class="muted">'+fmtINR(o.spend||0)+' &middot; '+rg(o.roas)+'</div></div>';}
function hdOverview(d){
  const o=d.overview||{};
  let h='<section class="section"><div class="kpis" style="grid-template-columns:repeat(4,1fr)">';
  h+=hdKpi('Running Budget',fmtINR(o.budget),'effective ₹/day');
  h+=hdKpi('Spend',fmtINR(o.spend),(d.since||'')+' → '+(d.until||''),'META');
  h+=hdKpi('ROAS',rg(o.roas),'pixel revenue / spend','PIXEL');
  h+=hdKpi('Orders',fmtNum(o.orders),fmtINR(o.revenue)+' revenue','PIXEL');
  h+='</div></section>';
  h+='<section class="section"><div class="card"><h3>Website-wise overview</h3><table><thead><tr><th>Website</th><th>Budget/day</th><th>Spend</th><th>ROAS</th><th>Orders</th><th>Campaigns</th></tr></thead><tbody>';
  (d.websites||[]).forEach(w=>{h+='<tr><td><span class="chip">'+hdEsc(w.name)+'</span></td><td>'+fmtINR(w.budget)+'</td><td>'+fmtINR(w.spend)+'</td><td>'+rg(w.roas)+'</td><td>'+fmtNum(w.orders)+'</td><td>'+fmtNum(w.campaigns)+'</td></tr>';});
  h+='</tbody></table></div></section>';
  h+='<section class="section"><div class="card"><h3>Products snapshot</h3><table><thead><tr><th>Product</th><th>Budget/day</th><th>Spend</th><th>ROAS</th><th>Orders</th><th>Campaigns</th></tr></thead><tbody>';
  (d.products||[]).forEach(p=>{h+='<tr><td><b>'+hdEsc(p.product)+'</b></td><td>'+fmtINR(p.budget)+'</td><td>'+fmtINR(p.spend)+'</td><td>'+rg(p.roas)+'</td><td>'+fmtNum(p.orders)+'</td><td>'+fmtNum(p.campaigns)+'</td></tr>';});
  h+='</tbody></table></div></section>';
  return h;
}
function hdBudget(d){
  let h='<section class="section"><div class="card"><h3>Product-wise budget allocation</h3><table><thead><tr><th>Product</th><th>Budget/day</th><th>Share</th><th>Spend</th><th>ROAS</th><th>Orders</th><th>Camps</th><th>Ad sets</th></tr></thead><tbody>';
  (d.products||[]).forEach(p=>{h+='<tr><td><b>'+hdEsc(p.product)+'</b></td><td>'+fmtINR(p.budget)+'</td>'+
    '<td><div style="display:flex;align-items:center;gap:8px;min-width:130px"><div style="flex:1;height:7px;background:var(--line,#e6e9f2);border-radius:4px;overflow:hidden"><div style="width:'+(p.budget_share||0)+'%;height:100%;background:#6366f1"></div></div><span class="muted" style="font-size:11px">'+(p.budget_share||0)+'%</span></div></td>'+
    '<td>'+fmtINR(p.spend)+'</td><td>'+rg(p.roas)+'</td><td>'+fmtNum(p.orders)+'</td><td>'+fmtNum(p.campaigns)+'</td><td>'+fmtNum(p.adsets)+'</td></tr>';});
  h+='</tbody></table></div></section>';
  return h;
}
function hdCampaigns(d){
  const cs=d.campaigns||[];
  let h='<section class="section"><div class="card"><h3>Campaigns ('+cs.length+')</h3><div class="csub">click a campaign &rarr; ad sets &rarr; creatives</div>';
  cs.forEach(c=>{
    h+='<details style="border-bottom:1px solid var(--line,#e6e9f2);padding:7px 0"><summary style="cursor:pointer"><b>'+hdEsc(c.name)+'</b> <span class="muted" style="font-family:monospace;font-size:11px">'+c.id+'</span><br><span class="muted" style="font-size:12px">'+c.adset_count+' ad sets &middot; '+fmtINR(c.budget)+'/day &middot; '+fmtINR(c.spend)+' spend &middot; '+rg(c.roas)+' &middot; '+fmtNum(c.orders)+' orders</span></summary><div style="padding:8px 0 4px 14px">';
    (c.adsets||[]).forEach(a=>{
      h+='<details style="margin:4px 0"><summary style="cursor:pointer">'+hdEsc(a.name)+' <span class="muted" style="font-family:monospace;font-size:11px">'+a.id+'</span> &middot; '+rg(a.roas)+' &middot; '+fmtNum(a.purchases)+' purch &middot; '+fmtINR(a.spend)+' &middot; '+(a.ads||[]).length+' creatives</summary><div style="display:flex;gap:10px;flex-wrap:wrap;padding:10px 0 6px">'+((a.ads||[]).map(hdCard).join('')||'<span class="muted">no ads</span>')+'</div></details>';
    });
    h+='</div></details>';
  });
  h+='</div></section>';
  return h;
}
function hdCreative(d){
  const cr=d.creative_report||{};
  const grid=items=>'<div style="display:flex;gap:10px;flex-wrap:wrap">'+((items||[]).map(hdCard).join('')||'<span class="muted">none above &#8377;'+(cr.min_spend||0)+' spend</span>')+'</div>';
  let h='<section class="section"><div class="card"><h3>Format performance</h3><div class="grid4" style="margin-top:10px">';
  h+=hdFmtCard('Paras Videos',cr.paras)+hdFmtCard('Motion',(cr.motion_still||{}).Motion)+hdFmtCard('Still / Static',(cr.motion_still||{}).Static)+hdFmtCard('UGC',cr.ugc);
  h+='</div></div></section>';
  h+='<section class="section"><div class="card"><h3>&#127942; Winning creatives</h3>'+grid(cr.winning)+'</div></section>';
  h+='<section class="section"><div class="card"><h3>&#128201; Losing creatives</h3>'+grid(cr.losing)+'</div></section>';
  h+='<section class="section"><div class="card"><h3>&#128640; Creative requirement for future scaling</h3><table><thead><tr><th>Product</th><th>ROAS</th><th>Spend</th><th>Ads</th><th>Missing formats</th><th>Recommendation</th></tr></thead><tbody>';
  (cr.scaling||[]).forEach(s=>{h+='<tr><td><b>'+hdEsc(s.product)+'</b></td><td>'+rg(s.roas)+'</td><td>'+fmtINR(s.spend)+'</td><td>'+fmtNum(s.ad_count)+'</td><td class="muted">'+hdEsc((s.missing||[]).join(', ')||'—')+'</td><td style="font-size:12px">'+hdEsc(s.recommendation)+'</td></tr>';});
  h+='</tbody></table></div></section>';
  return h;
}
function hdClearance(d){
  const ps=(d.products||[]).slice().sort((a,b)=>(a.roas||0)-(b.roas||0));
  const sig=r=> r<1.0?['#d4434a','Liquidate — ad spend losing money'] : r<1.8?['#c47a00','Clear slow stock — cut budget / discount'] : ['#1a9d52','Healthy — keep'];
  const cands=ps.filter(p=>(p.roas||0)<1.8&&(p.spend||0)>0);
  const spendBehind=cands.reduce((s,p)=>s+(p.spend||0),0);
  let h='<section class="section"><div class="kpis" style="grid-template-columns:repeat(3,1fr)">';
  h+=hdKpi('Clearance candidates',fmtNum(cands.length),'products with ROAS &lt; 1.8×');
  h+=hdKpi('Products',fmtNum(ps.length),'live in home decor');
  h+=hdKpi('Spend behind them',fmtINR(spendBehind),'on sub-1.8× products');
  h+='</div></section>';
  h+='<section class="section"><div class="card"><h3>Clearance priority (worst ROAS first)</h3><div class="csub">Heuristic from ad performance &mdash; no live inventory feed yet. Low ROAS + spend = candidates to clear / wind down. Wire Shopify inventory for true days-of-cover.</div><table><thead><tr><th>Product</th><th>ROAS</th><th>Spend</th><th>Orders</th><th>Budget/day</th><th>Signal</th></tr></thead><tbody>';
  ps.forEach(p=>{const s=sig(p.roas||0);h+='<tr><td><b>'+hdEsc(p.product)+'</b></td><td>'+rg(p.roas)+'</td><td>'+fmtINR(p.spend)+'</td><td>'+fmtNum(p.orders)+'</td><td>'+fmtINR(p.budget)+'</td><td style="color:'+s[0]+';font-size:12px">'+s[1]+'</td></tr>';});
  h+='</tbody></table></div></section>';
  return h;
}
function hdProfit(d){
  const ps=(d.products||[]).map(p=>{const profit=(p.revenue||0)-(p.spend||0);return Object.assign({},p,{profit:profit,margin:p.revenue>0?100*profit/p.revenue:0});}).sort((a,b)=>b.profit-a.profit);
  const tRev=ps.reduce((s,p)=>s+(p.revenue||0),0), tSpend=ps.reduce((s,p)=>s+(p.spend||0),0), tProfit=tRev-tSpend;
  const pc=v=>'<span style="color:'+(v>=0?'#1a9d52':'#d4434a')+';font-weight:700">'+fmtINR(v)+'</span>';
  let h='<section class="section"><div class="kpis" style="grid-template-columns:repeat(4,1fr)">';
  h+=hdKpi('Revenue',fmtINR(tRev),'pixel-attributed','PIXEL');
  h+=hdKpi('Ad Spend',fmtINR(tSpend),'Meta','META');
  h+=hdKpi('Ad Contribution',pc(tProfit),'revenue − ad spend');
  h+=hdKpi('Margin',(tRev>0?100*tProfit/tRev:0).toFixed(1)+'%','contribution / revenue');
  h+='</div></section>';
  h+='<section class="section"><div class="card"><h3>Product profitability</h3><div class="csub">Ad contribution = Pixel Revenue &minus; Ad Spend. Excludes COGS / product cost &mdash; add a cost sheet for true net profit.</div><table><thead><tr><th>Product</th><th>Spend</th><th>Revenue</th><th>Ad Contribution</th><th>Margin</th><th>ROAS</th><th>Orders</th></tr></thead><tbody>';
  ps.forEach(p=>{h+='<tr><td><b>'+hdEsc(p.product)+'</b></td><td>'+fmtINR(p.spend)+'</td><td>'+fmtINR(p.revenue)+'</td><td>'+pc(p.profit)+'</td><td>'+p.margin.toFixed(1)+'%</td><td>'+rg(p.roas)+'</td><td>'+fmtNum(p.orders)+'</td></tr>';});
  h+='</tbody></table></div></section>';
  return h;
}
function hdRender(){
  document.getElementById('hdTabs').innerHTML=HD_TABS.map(t=>'<button data-t="'+t[0]+'"'+(t[0]===HDTAB?' class="on"':'')+'>'+t[1]+'</button>').join('');
  [...document.querySelectorAll('#hdTabs button')].forEach(b=>b.onclick=()=>{HDTAB=b.dataset.t;hdRender();});
  const presets=Object.keys(HD_PRESET_LABELS).filter(p=>HOMEDECOR&&HOMEDECOR[p]);
  document.getElementById('hdPresetSeg').innerHTML=(presets.length?presets:['yesterday']).map(p=>'<button data-p="'+p+'"'+(p===HDPRESET?' class="on"':'')+'>'+HD_PRESET_LABELS[p]+'</button>').join('');
  [...document.querySelectorAll('#hdPresetSeg button')].forEach(b=>b.onclick=()=>{HDPRESET=b.dataset.p;hdRender();});
  const d=hdPick(), body=document.getElementById('hdBody'), fp=document.getElementById('hdFetched');
  if(!d){ document.getElementById('hdScope').textContent=''; fp.textContent='';
    body.innerHTML='<section class="section"><div class="card"><div class="muted">Home-Decor data is not embedded in this build (no Meta token at build time). It will populate on the next scheduled build.</div></div></section>'; return; }
  document.getElementById('hdScope').textContent='('+(d.since||'')+' → '+(d.until||'')+')';
  fp.innerHTML='&#128336; Fetched from Meta: <b>'+hdEsc(d.fetched_at||'—')+'</b>'+(d.fetched_ts?' ('+hdAgo(d.fetched_ts)+')':'');
  body.innerHTML = HDTAB==='overview'?hdOverview(d) : HDTAB==='campaigns'?hdCampaigns(d) : HDTAB==='budget'?hdBudget(d) : HDTAB==='clearance'?hdClearance(d) : HDTAB==='profit'?hdProfit(d) : hdCreative(d);
}
setInterval(()=>{ if(STATE.view==='homedecor'){ const d=hdPick(), fp=document.getElementById('hdFetched'); if(d&&d.fetched_ts&&fp) fp.innerHTML='&#128336; Fetched from Meta: <b>'+hdEsc(d.fetched_at||'—')+'</b> ('+hdAgo(d.fetched_ts)+')'; } }, 60000);
// ===================== /HOME DECOR MODULE =====================

function renderAll(){
  if(STATE.view==='homedecor'){ hdRender(); }
  else if(STATE.view==='category'){ renderCategory(); }
  else { renderHero(); renderPrepaid(); renderCat(); renderCreative(); }
}

function showView(view){
  STATE.view=view;
  document.getElementById('view-home').style.display = view==='home'?'':'none';
  document.getElementById('view-category').style.display = view==='category'?'':'none';
  document.getElementById('view-homedecor').style.display = view==='homedecor'?'':'none';
  const crumb=document.querySelector('.crumb');
  crumb.innerHTML = view==='homedecor'
    ? 'Crystal Home Decor<small>category module &middot; live from Meta (embedded)</small>'
    : view==='category'
    ? (CAT_LABELS[STATE.cat]||STATE.cat)+'<small>category deep-dive &middot; spend real, orders/ROAS pixel</small>'
    : 'Home<small>Live sales, ROAS &amp; breakdowns</small>';
  if(view==='category'||view==='homedecor') window.scrollTo({top:0});
  renderAll();
}

// portal segmented control
(function(){
  const seg=document.getElementById('portalseg');
  ['ALL'].concat(PAYLOAD.portals).forEach((p,i)=>{const b=document.createElement('button');
    b.textContent=p; if(p===STATE.portal)b.className='on';
    b.onclick=()=>{STATE.portal=p;[...seg.children].forEach(c=>c.classList.remove('on'));b.classList.add('on');renderAll();};
    seg.appendChild(b);});
})();

// calendar (home + category share one range)
const dFrom=document.getElementById('dFrom'), dTo=document.getElementById('dTo');
const cdFrom=document.getElementById('cdFrom'), cdTo=document.getElementById('cdTo');
[dFrom,dTo,cdFrom,cdTo].forEach(el=>{el.min=PAYLOAD.minDate;el.max=PAYLOAD.maxDate;});
dFrom.value=cdFrom.value=STATE.from; dTo.value=cdTo.value=STATE.to;
function setRange(from,to){
  STATE.from=from;STATE.to=to;
  dFrom.value=cdFrom.value=from; dTo.value=cdTo.value=to;
  renderChrome(); renderAll();
}
dFrom.onchange=()=>setRange(dFrom.value,STATE.to);
dTo.onchange=()=>setRange(STATE.from,dTo.value);
cdFrom.onchange=()=>setRange(cdFrom.value,STATE.to);
cdTo.onchange=()=>setRange(STATE.from,cdTo.value);

// sidebar: Home items scroll; category items open a deep-dive view
document.querySelectorAll('.item').forEach(it=>it.onclick=()=>{
  document.querySelectorAll('.item').forEach(x=>x.classList.remove('active'));it.classList.add('active');
  if(it.dataset.cat){ STATE.cat=it.dataset.cat;
    showView(it.dataset.cat==='Crystal Home Decor' ? 'homedecor' : 'category'); }
  else { showView('home'); const t=document.getElementById(it.dataset.go); if(t) t.scrollIntoView({behavior:'smooth',block:'start'}); }
});

// sortable headers
function wireSort(tableId, sortObj, render){
  document.querySelectorAll('#'+tableId+' thead th').forEach(th=>th.onclick=()=>{
    const k=th.dataset.k; if(sortObj.k===k)sortObj.dir*=-1; else {sortObj.k=k;sortObj.dir=-1;} render();});
}
wireSort('catTable',catSort,renderCat);
wireSort('creativeTable',ctSort,renderCreative);
wireSort('catCreativeTable',catCtSort,renderCategory);

// live feed (optional): set window.ANTARIKSH_LIVE_URL to a Worker/KV endpoint
async function pollLive(){
  const url=window.ANTARIKSH_LIVE_URL; if(!url)return;
  try{const r=await fetch(url,{cache:'no-store'});if(!r.ok)return;const j=await r.json();
    STATE.live=j.byPortal||j; document.getElementById('heroscope').textContent=' - live, today';
    renderHero();
    const fp=document.getElementById('freshpill');fp.className='pill';
    document.getElementById('freshtxt').textContent='live - '+(j.generated_at||'just now');
  }catch(e){/* keep fallback */}
}

document.getElementById('foot').textContent='span '+PAYLOAD.minDate+' .. '+PAYLOAD.maxDate;
renderChrome(); renderAll();
pollLive(); setInterval(pollLive, 5*60*1000);
</script>
</body>
</html>
"""


if __name__ == '__main__':
    main()
