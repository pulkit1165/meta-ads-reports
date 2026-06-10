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
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _utils import db_connect, IST, DEFAULT_DB  # noqa: E402
from build_antariksh_rollup import fallback_category  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_OUT = REPO_ROOT / 'antariksh' / 'index.html'

# Profit-model anchors (operator spec): blended ROI → Profit%.
# breakeven ~1.8 (0%), 2.2 → 10%, 2.6 → 20%. Editable in the UI; these are
# just the embedded defaults so the page renders sensibly before any tweak.
PROFIT_ANCHORS = {'p10': 2.2, 'p20': 2.6}
GOAL_ORDERS_PER_DAY = 5000


def fetch_ad_days(conn, since):
    """Meta spend pre-aggregated to (date, portal, category). The Profit First
    view only needs real spend split by category — not per-ad rows or pixel
    revenue — so this stays small (a few hundred rows vs tens of thousands)."""
    rows = conn.execute('''
        SELECT d.date, d.portal,
               COALESCE(NULLIF(TRIM(m.category),''),'Other') AS cat,
               ROUND(SUM(COALESCE(d.spend,0)),2)             AS spend
        FROM meta_ads_daily d
        LEFT JOIN meta_ads_meta m ON d.ad_id = m.ad_id
        WHERE d.spend > 0 AND d.date >= ?
        GROUP BY d.date, d.portal, cat
        ORDER BY d.date
    ''', (since,)).fetchall()
    return [{'d': r[0], 'p': r[1], 'c': r[2], 's': r[3]} for r in rows]


def fetch_shop_days(conn, since):
    rows = conn.execute('''
        SELECT date, portal, orders, revenue, prepaid_orders, cod_orders
        FROM antariksh_shopify_daily
        WHERE date >= ?
        ORDER BY date
    ''', (since,)).fetchall()
    return [{'d': r[0], 'p': r[1], 'o': r[2], 'rev': r[3],
             'pp': r[4], 'cod': r[5]} for r in rows]


def fetch_cat_rev(conn, since):
    """REAL Shopify per-day category revenue (line-item, ground truth) from the
    antariksh_category_daily rollup. `src` lets the UI show mapping coverage."""
    rows = conn.execute('''
        SELECT date, portal, category, source, revenue, units, orders
        FROM antariksh_category_daily
        WHERE date >= ?
        ORDER BY date
    ''', (since,)).fetchall()
    return [{'d': r[0], 'p': r[1], 'c': r[2], 'src': r[3],
             'rev': r[4], 'u': r[5], 'o': r[6]} for r in rows]


def fetch_products(conn, top=60):
    """Top products by last-30d Shopify line revenue, with prev-30d revenue (for
    trend / life-cycle) and first-order date (launch proxy). Category resolved
    the same way as the rollup: master sheet first, name fallback otherwise.

    Product reports are inherently a recent-performance view, so they use a fixed
    last-30-day window (not the calendar) to keep the payload lean."""
    maxd = conn.execute(
        "SELECT MAX(substr(created_at,1,10)) FROM shopify_orders").fetchone()[0]
    if not maxd:
        return {'rows': [], 'curFrom': '', 'curTo': '', 'prevFrom': '', 'prevTo': ''}
    until = date.fromisoformat(maxd)
    cur_s = (until - timedelta(days=29)).isoformat()
    prev_e = (until - timedelta(days=30)).isoformat()
    prev_s = (until - timedelta(days=59)).isoformat()

    labels = {sku: cat for sku, cat in conn.execute(
        "SELECT ntn_code, TRIM(category) FROM product_ntn_labels "
        "WHERE TRIM(COALESCE(category,'')) != ''").fetchall()}

    def agg_by_sku(since_, until_):
        out = {}
        for sku, title, rev, u, o in conn.execute('''
            SELECT oi.sku, oi.product_title,
                   SUM(oi.line_revenue), SUM(oi.quantity), COUNT(DISTINCT oi.order_id)
            FROM shopify_order_items oi
            JOIN shopify_orders o ON oi.order_id=o.order_id AND oi.portal=o.portal
            WHERE substr(o.created_at,1,10) BETWEEN ? AND ? AND o.cancelled_at IS NULL
            GROUP BY oi.sku, oi.product_title
        ''', (since_, until_)).fetchall():
            d = out.setdefault(sku, {'rev': 0.0, 'u': 0, 'o': 0, 'titles': {}})
            d['rev'] += rev or 0
            d['u'] += u or 0
            d['o'] += o or 0
            d['titles'][title or sku] = d['titles'].get(title or sku, 0) + (rev or 0)
        return out

    cur = agg_by_sku(cur_s, maxd)
    prev = agg_by_sku(prev_s, prev_e)
    top_skus = sorted(cur, key=lambda s: cur[s]['rev'], reverse=True)[:top]

    fo = {}
    if top_skus:
        ph = ','.join('?' * len(top_skus))
        fo = dict(conn.execute(
            f"SELECT sku, MIN(substr(created_at,1,10)) FROM shopify_order_items oi "
            f"JOIN shopify_orders o ON oi.order_id=o.order_id AND oi.portal=o.portal "
            f"WHERE sku IN ({ph}) GROUP BY sku", top_skus).fetchall())

    rows = []
    for sku in top_skus:
        d = cur[sku]
        title = max(d['titles'], key=d['titles'].get) if d['titles'] else sku
        cat = labels.get(sku) or fallback_category(title) or 'Other'
        first = fo.get(sku)
        days_live = (until - date.fromisoformat(first)).days if first else None
        rows.append({
            'sku': sku, 'name': title, 'c': cat,
            'rev': round(d['rev'], 2), 'u': d['u'], 'o': d['o'],
            'rev0': round(prev.get(sku, {}).get('rev', 0.0), 2),
            'fo': first, 'live': days_live,
        })
    return {'rows': rows, 'curFrom': cur_s, 'curTo': maxd,
            'prevFrom': prev_s, 'prevTo': prev_e}


def build_payload(conn, days):
    since = (datetime.now(IST).date() - timedelta(days=days - 1)).isoformat()
    ad_days = fetch_ad_days(conn, since)
    shop = fetch_shop_days(conn, since)
    cat_rev = fetch_cat_rev(conn, since)
    products = fetch_products(conn)
    dates = [r['d'] for r in shop] + [r['d'] for r in ad_days]
    portals = sorted({r['p'] for r in shop} | {r['p'] for r in ad_days})
    return {
        'generated_at': datetime.now(IST).isoformat(),
        'today': datetime.now(IST).date().isoformat(),
        'minDate': min(dates) if dates else since,
        'maxDate': max(dates) if dates else since,
        'portals': portals,
        'anchors': PROFIT_ANCHORS,
        'goalOrders': GOAL_ORDERS_PER_DAY,
        'adDays': ad_days,
        'shop': shop,
        'catRev': cat_rev,
        'products': products,
    }


HD_PRESETS = ['today', 'yesterday', 'last_7d', 'last_30d']

# Every category that gets the rich live-Meta module (same tabs as Crystal Home
# Decor). Crystal is FIRST so it warms the shared account-level caches in
# home_decor_dashboard_data; every other category then reuses them cheaply.
# Categories not listed here (Other / DS) keep the generic Profit-First deep-dive.
HD_CATEGORIES = [
    'Crystal Home Decor', 'Skin', 'Hair', '24K Jewellery',
    'Crystal Accessory', 'Perfumes', 'Nutraceuticals', 'Aibot',
]


def build_categories(presets=HD_PRESETS, categories=HD_CATEGORIES):
    """Rich per-category payloads (campaigns -> ad sets -> creatives, product +
    website rollups, creative report, closed budget), fetched live from Meta at
    build time and embedded as {category: {preset: payload}} so the browser makes
    zero API calls. Degrades to {} when META_ACCESS_TOKEN is absent; per
    category/preset errors are isolated so one bad slice never blocks the rest or
    the main build."""
    import os
    out = {}
    if not os.getenv('META_ACCESS_TOKEN'):
        print("  categories: META_ACCESS_TOKEN not set — embedding empty payload",
              file=sys.stderr)
        return out
    try:
        import home_decor_dashboard_data as hdd
    except Exception as e:  # noqa: BLE001
        print(f"  categories: import failed ({e}) — embedding empty", file=sys.stderr)
        return out
    for cat in categories:
        cat_out = {}
        for p in presets:
            try:
                cat_out[p] = hdd.build(p, category=cat)
                ov = cat_out[p].get('overview', {})
                print(f"  cat[{cat}][{p}]: {ov.get('campaigns', 0)} camps, "
                      f"Rs {ov.get('budget', 0):,}/day", file=sys.stderr)
            except Exception as e:  # noqa: BLE001
                print(f"  cat[{cat}][{p}] failed: {e}", file=sys.stderr)
        if cat_out:
            out[cat] = cat_out
    return out


def render(payload, catdata=None):
    data = json.dumps(payload, separators=(',', ':'))
    cd = json.dumps(catdata or {}, separators=(',', ':'), ensure_ascii=False)
    return (HTML.replace('/*__PAYLOAD__*/', data)
                .replace('/*__CATDATA__*/', cd))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', default=str(DEFAULT_DB))
    ap.add_argument('--days', type=int, default=90)
    ap.add_argument('--out', default=str(DEFAULT_OUT))
    args = ap.parse_args()

    conn = db_connect(Path(args.db))
    payload = build_payload(conn, args.days)
    conn.close()

    catdata = build_categories()

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(payload, catdata))
    kb = out.stat().st_size / 1024
    print(f"wrote {out}  ({kb:.0f} KB)  "
          f"{len(payload['adDays'])} ad-days, {len(payload['shop'])} shop-days, "
          f"span {payload['minDate']}..{payload['maxDate']}")


HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Antariksh - Profit First Reports</title>
<style>
  :root{
    --bg:#f4f6fa;--panel:#fff;--ink:#161d2b;--muted:#6b7686;--line:#e6e9ef;
    --brand:#2f5bff;--brand-soft:#eaf0ff;
    --good:#0c9b5b;--bad:#d23b3b;--warn:#c98a00;--blue:#2f5bff;
    --scale:#0c9b5b;--maintain:#2f6bff;--reduce:#c98a00;--exit:#d23b3b;
    --shadow:0 1px 2px rgba(16,24,40,.05),0 1px 3px rgba(16,24,40,.08);
  }
  *{box-sizing:border-box}
  html,body{margin:0;height:100%}
  body{font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,Roboto,Arial,sans-serif;color:var(--ink);background:var(--bg)}
  .app{display:grid;grid-template-columns:236px 1fr;min-height:100vh}
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
  .topbar{position:sticky;top:0;z-index:5;background:rgba(244,246,250,.92);backdrop-filter:blur(8px);border-bottom:1px solid var(--line);padding:11px 22px;display:flex;align-items:center;gap:10px;flex-wrap:wrap}
  .crumb{font-weight:700;font-size:16px}.crumb small{display:block;font-weight:500;color:var(--muted);font-size:12px;margin-top:1px}
  .spacer{flex:1}
  .seg{display:flex;background:var(--panel);border:1px solid var(--line);border-radius:9px;overflow:hidden;box-shadow:var(--shadow)}
  .seg button{border:0;background:transparent;padding:7px 11px;font-size:12.5px;cursor:pointer;color:var(--muted);font-weight:600}
  .seg button.on{background:var(--brand);color:#fff}
  .pill{display:inline-flex;align-items:center;gap:7px;background:var(--panel);border:1px solid var(--line);border-radius:999px;padding:6px 12px;font-size:12px;box-shadow:var(--shadow)}
  .pill .led{width:8px;height:8px;border-radius:50%;background:var(--good)}
  .pill.amber .led{background:var(--warn)}.pill.red .led{background:var(--bad)}
  .pill.range{color:var(--muted);font-weight:600}

  .banner{background:linear-gradient(100deg,#10204d,#2f5bff);color:#fff;border-radius:14px;padding:16px 20px;margin:0 0 20px;box-shadow:var(--shadow)}
  .banner .t{font-size:12px;letter-spacing:.16em;font-weight:800;opacity:.85}
  .banner .g{font-size:21px;font-weight:800;margin:4px 0 8px;letter-spacing:-.01em}
  .banner .roi{display:flex;gap:8px;flex-wrap:wrap}
  .banner .roi span{background:rgba(255,255,255,.15);border:1px solid rgba(255,255,255,.22);border-radius:999px;padding:3px 11px;font-size:12px;font-weight:600}
  .footbanner{background:#0f1830;color:#cdd5e3;border-radius:14px;padding:14px 20px;margin:6px 0 30px;font-size:13.5px;font-weight:700;letter-spacing:.02em;text-align:center}
  .footbanner b{color:#7fd3a6}

  .livebar{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:14px 18px;margin:0 0 18px;box-shadow:var(--shadow)}
  .livebar .lh{font-size:12px;font-weight:800;letter-spacing:.12em;display:flex;align-items:center;gap:8px;color:var(--bad);margin-bottom:10px}
  .livebar .lh .led{width:9px;height:9px;border-radius:50%;background:var(--bad);box-shadow:0 0 0 0 rgba(214,59,59,.55);animation:livepulse 1.8s infinite}
  .livebar .lh small{color:var(--muted);font-weight:600;letter-spacing:0;text-transform:none}
  .livekpis{display:grid;grid-template-columns:repeat(4,1fr);gap:14px}
  .livekpis .lk .lkl{font-size:11.5px;color:var(--muted);font-weight:600}
  .livekpis .lk .lkv{font-size:25px;font-weight:800;margin-top:3px;letter-spacing:-.01em}
  @keyframes livepulse{0%{box-shadow:0 0 0 0 rgba(214,59,59,.5)}70%{box-shadow:0 0 0 7px rgba(214,59,59,0)}100%{box-shadow:0 0 0 0 rgba(214,59,59,0)}}
  @media(max-width:720px){.livekpis{grid-template-columns:repeat(2,1fr)}}

  .dutybanner{overflow:hidden;background:linear-gradient(90deg,#eef2ff,#f0fdf4);border:1px solid var(--line);border-radius:12px;padding:9px 0;margin:0 0 14px;box-shadow:var(--shadow)}
  .dutytrack{display:inline-flex;white-space:nowrap;animation:dutyscroll 34s linear infinite;will-change:transform}
  .dutybanner:hover .dutytrack{animation-play-state:paused}
  .duty{padding:0 4px;font-weight:700;font-size:12.5px;color:var(--ink)}
  .duty b{color:var(--bad)}
  .duty .sep{color:var(--blue);margin:0 6px;font-weight:900}
  @keyframes dutyscroll{from{transform:translateX(0)}to{transform:translateX(-50%)}}

  .fbox{background:linear-gradient(90deg,#fff7ed,#eff6ff);border:1px solid var(--line);border-left:4px solid var(--blue);border-radius:10px;padding:11px 14px;margin:0 0 14px;font-size:13px;line-height:1.5}
  .fbox b{color:var(--blue)}
  .crq{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}
  .crq .cb{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:16px;box-shadow:var(--shadow)}
  .crq .cb h4{margin:0;font-size:14.5px;display:flex;align-items:center;gap:7px}
  .crq .cb .goal{font-size:11px;color:var(--muted);font-weight:700;text-transform:uppercase;letter-spacing:.05em;margin:3px 0 10px}
  .crq .row{display:flex;justify-content:space-between;align-items:center;padding:5px 0;border-bottom:1px dashed var(--line);font-size:13px}
  .crq .row:last-of-type{border-bottom:0}
  .crq .row .k{color:var(--muted)}
  .crq input.tin{width:98px;font-size:13px;padding:4px 6px;border:1px solid var(--line);border-radius:6px;background:var(--panel);color:inherit;text-align:right}
  .crq .cr{margin-top:10px;text-align:center;background:#f7f9ff;border:1px solid var(--line);border-radius:10px;padding:10px}
  .crq .cr .n{font-size:30px;font-weight:800;line-height:1}
  .crq .cr .l{font-size:10.5px;color:var(--muted);font-weight:600;margin-top:3px}
  @media(max-width:760px){.crq{grid-template-columns:1fr}}

  .wrap{padding:22px;max-width:1240px;width:100%}
  .section{margin-bottom:24px;scroll-margin-top:80px}
  .section>h2{font-size:13px;letter-spacing:.05em;text-transform:uppercase;color:var(--muted);margin:0 0 12px;display:flex;align-items:center;gap:8px}
  .section>h2 .n{display:inline-grid;place-items:center;width:20px;height:20px;border-radius:6px;background:var(--brand-soft);color:var(--brand);font-size:11px;font-weight:800}

  .kpis{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}
  .kpis.k6{grid-template-columns:repeat(6,1fr)}
  .kpi{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:16px;box-shadow:var(--shadow)}
  .kpi .lbl{font-size:11.5px;color:var(--muted);font-weight:600;display:flex;align-items:center;gap:6px}
  .kpi .val{font-size:26px;font-weight:760;margin:7px 0 2px;letter-spacing:-.01em}
  .kpi .sub{font-size:11.5px;color:var(--muted)}
  .tag{font-size:9.5px;font-weight:700;padding:1px 6px;border-radius:6px;background:var(--brand-soft);color:var(--brand)}
  .tag.gt{background:#e7f7ee;color:var(--good)}.tag.px{background:#fdeaea;color:var(--bad)}
  .tag.est{background:#fff7e6;color:var(--warn)}

  .grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px}
  .grid3{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}
  .grid4{display:grid;grid-template-columns:repeat(4,1fr);gap:14px}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:16px 18px;box-shadow:var(--shadow)}
  .card h3{margin:0 0 2px;font-size:14.5px}.card .csub{color:var(--muted);font-size:12px;margin-bottom:12px}
  .ph::after{content:"needs data feed";position:absolute;top:14px;right:16px;font-size:10px;font-weight:700;color:var(--warn);background:#fff7e6;border:1px solid #ffe3a3;padding:2px 7px;border-radius:6px}
  .ph{position:relative}
  .big{font-size:26px;font-weight:750}.muted{color:var(--muted)}

  table{width:100%;border-collapse:collapse;font-size:13px}
  th,td{padding:8px 10px;text-align:right;border-bottom:1px solid var(--line);white-space:nowrap}
  th:first-child,td:first-child{text-align:left}
  thead th{font-size:11px;letter-spacing:.04em;text-transform:uppercase;color:var(--muted);font-weight:700;cursor:pointer;user-select:none}
  tbody tr:hover{background:#fafbff}
  .chip{display:inline-block;padding:2px 9px;border-radius:999px;font-size:11.5px;background:#eef1f6;color:#42506b;font-weight:600}
  .rg{font-weight:700}.rg.g{color:var(--good)}.rg.m{color:var(--warn)}.rg.b{color:var(--bad)}
  .st{display:inline-block;padding:2px 9px;border-radius:999px;font-size:10.5px;font-weight:800;letter-spacing:.04em;color:#fff}
  .st.scale{background:var(--scale)}.st.maintain{background:var(--maintain)}.st.reduce{background:var(--reduce)}.st.exit{background:var(--exit)}

  .bar{height:9px;border-radius:6px;background:#eef1f6;overflow:hidden;display:flex}
  .bar > i{display:block;height:100%}
  .alloc-row{display:grid;grid-template-columns:150px 1fr 96px;gap:10px;align-items:center;margin:9px 0}
  .alloc-row .nm{font-size:12.5px;font-weight:600}
  .progress{height:14px;border-radius:8px;background:#eef1f6;overflow:hidden;margin-top:6px}
  .progress > i{display:block;height:100%;background:linear-gradient(90deg,#2f5bff,#0c9b5b)}
  .decision{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}
  .dcol{border:1px solid var(--line);border-radius:12px;padding:12px;background:#fff}
  .dcol h4{margin:0 0 8px;font-size:12px;letter-spacing:.04em;text-transform:uppercase;display:flex;align-items:center;gap:6px}
  .dcol .dot{width:9px;height:9px;border-radius:50%}
  .dcol .di{display:flex;justify-content:space-between;gap:8px;font-size:12.5px;padding:4px 0;border-top:1px dashed var(--line)}
  .dcol .di:first-of-type{border-top:0}
  .rank{display:flex;align-items:center;gap:10px;padding:7px 0;border-bottom:1px solid var(--line)}
  .rank .num{width:22px;height:22px;border-radius:7px;background:var(--brand-soft);color:var(--brand);font-weight:800;display:grid;place-items:center;font-size:12px}
  .rank .nm{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:13px;font-weight:600}
  .rank .mt{font-size:12px;color:var(--muted);text-align:right}

  .cal{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
  .cal input[type=date]{border:1px solid var(--line);border-radius:8px;padding:6px 9px;font:inherit;background:var(--panel)}
  .inp{border:1px solid var(--line);border-radius:8px;padding:6px 9px;font:inherit;width:120px;text-align:right}
  .note{background:#fff7e6;border:1px solid #ffe3a3;color:#7a5500;border-radius:10px;padding:9px 13px;font-size:12.5px;margin-bottom:16px}
  .cover{font-size:11.5px;color:var(--muted);margin-top:8px}
  .cover b{color:var(--ink)}
  @media(max-width:1024px){.app{grid-template-columns:1fr}.side{display:none}.kpis,.kpis.k6{grid-template-columns:repeat(2,1fr)}.grid2,.grid3,.grid4,.decision,.alloc-row{grid-template-columns:1fr}}
</style>
</head>
<body>
<div class="app">
  <aside class="side">
    <div class="brand"><div class="logo">A</div><div><b>Antariksh</b><small>Profit First reports</small></div></div>
    <nav class="nav">
      <div class="grp">Company</div>
      <div class="item active" data-view="home"><span class="ic">&#9670;</span> All categories</div>
      <div class="grp">By category</div>
      <div id="catnav"></div>
      <div class="grp">Reports</div>
      <div class="item" data-go="r1"><span class="ic">1</span> Company Overview</div>
      <div class="item" data-go="r2"><span class="ic">2</span> Category Budget</div>
      <div class="item" data-go="r4"><span class="ic">4</span> Profit Contribution</div>
      <div class="item" data-go="r6"><span class="ic">6</span> Budget In Hand</div>
      <div class="item" data-go="r9"><span class="ic">9</span> Decision Board</div>
    </nav>
    <div class="side-foot" id="foot">loading...</div>
  </aside>

  <div class="main">
    <div class="topbar">
      <div class="crumb" id="crumb">Company<small>profit-first view, all categories</small></div>
      <div class="spacer"></div>
      <div class="seg" id="portalseg"></div>
      <div class="pill range" id="rangepill">Range: -</div>
      <div class="pill" id="freshpill"><span class="led"></span> <span id="freshtxt">-</span></div>
    </div>

    <div class="wrap" id="reportWrap">
      <div class="banner">
        <div class="t">PROFIT FIRST REPORTING</div>
        <div class="g" id="bannerGoal">5,000 ORDERS / DAY WITH 20% PROFIT BEFORE SCALING</div>
        <div class="roi" id="bannerRoi"></div>
      </div>

      <div class="note" id="stalenote" style="display:none"></div>

      <!-- LIVE TODAY (latest embedded day, refreshes each build) -->
      <div class="livebar" id="liveBar" style="display:none"></div>

      <!-- 1. COMPANY / CATEGORY OVERVIEW -->
      <section class="section" id="r1">
        <h2><span class="n">1</span> <span id="t1">Company Overview</span> <span id="scope1" class="muted" style="text-transform:none;letter-spacing:0"></span></h2>
        <div class="kpis k6" id="ovKpis"></div>
        <div class="card" style="margin-top:14px">
          <h3 id="ovSplitTitle">Website split</h3>
          <div class="csub">Studd Muffyn (SM) &middot; Nuskhe By Paras (NBP) &middot; Studd Muffyn Life (SML) &mdash; revenue = Shopify ground truth, ROI = revenue / Meta spend</div>
          <table id="ovSplit"><thead><tr><th>Website</th><th>Revenue</th><th>Orders</th><th>Spend</th><th>ROI</th></tr></thead><tbody></tbody></table>
        </div>
      </section>

      <!-- 2. CATEGORY WISE BUDGET -->
      <section class="section" id="r2">
        <h2><span class="n">2</span> <span id="t2">Category Wise Budget</span></h2>
        <div class="card">
          <div style="display:flex;align-items:center;gap:14px;flex-wrap:wrap;margin-bottom:10px">
            <div><h3 style="margin:0" id="h2t">Spend, revenue, ROI &amp; profit by category</h3>
              <div class="csub" style="margin:0">spend = real Meta &middot; revenue = real Shopify (reconciled to net order total) &middot; profit% derived from ROI</div></div>
            <div class="spacer"></div>
            <div class="cal"><span class="muted">From</span><input type="date" id="dFrom"><span class="muted">To</span><input type="date" id="dTo"></div>
          </div>
          <table id="catTable"><thead><tr>
            <th data-k="cat">Category</th><th data-k="spend">Spend</th>
            <th data-k="net">Revenue</th><th data-k="units">Units</th>
            <th data-k="roi">ROI</th><th data-k="profit">Status</th>
          </tr></thead><tbody></tbody></table>
          <div class="cover" id="coverLine"></div>
        </div>
      </section>

      <!-- 3. CATEGORY BUDGET ALLOCATION -->
      <section class="section" id="r3">
        <h2><span class="n">3</span> <span id="t3">Category Budget Allocation</span></h2>
        <div class="card">
          <h3 id="h3t">Spend share vs revenue share</h3>
          <div class="csub">where the money goes (Meta spend) vs where the money comes from (Shopify revenue). Spend &gt; revenue share = over-invested.</div>
          <div id="allocWrap"></div>
        </div>
      </section>

      <!-- 4. PRODUCT PROFIT CONTRIBUTION -->
      <section class="section" id="r4">
        <h2><span class="n">4</span> <span id="t4">Product Profit Contribution</span> <span class="tag est">LAST 30D</span></h2>
        <div class="grid2">
          <div class="card"><h3>Top 10 by revenue</h3><div class="csub" id="prodWin">last 30 days &middot; gross product revenue</div><div id="rankWrap"></div></div>
          <div class="card"><h3>Profit contribution</h3><div class="csub">est. profit = revenue &times; category profit% &middot; share of total est. profit</div><div id="contribWrap"></div></div>
        </div>
      </section>

      <!-- 5. PRODUCT PROFITABILITY -->
      <section class="section" id="r5">
        <h2><span class="n">5</span> <span id="t5">Product Profitability</span> <span class="tag est">LAST 30D</span></h2>
        <div class="card">
          <h3>Scale / Maintain / Reduce / Exit</h3>
          <div class="csub">status from the product's category ROI (product-level ad spend not split yet). Trend = revenue vs previous 30 days.</div>
          <table id="profitTable"><thead><tr>
            <th data-k="name">Product</th><th data-k="c">Category</th>
            <th data-k="rev">Revenue 30d</th><th data-k="trend">Trend</th>
            <th data-k="roi">Cat ROI</th><th data-k="status">Status</th>
          </tr></thead><tbody></tbody></table>
        </div>
      </section>

      <!-- 6. BUDGET IN HAND -->
      <section class="section" id="r6">
        <h2><span class="n">6</span> <span id="t6">Budget In Hand</span> <span id="scope6" class="muted" style="text-transform:none;letter-spacing:0"></span></h2>
        <div class="grid2">
          <div class="card">
            <h3>Today&rsquo;s cash position</h3>
            <div class="csub">Today&rsquo;s profit (est.) &minus; outstanding dues = what&rsquo;s free to redeploy</div>
            <table id="bihTable"></table>
            <div class="big" id="bihVal" style="margin-top:10px"></div>
            <div class="muted" id="bihNote" style="font-size:12px"></div>
          </div>
          <div class="card">
            <h3>Dues (editable)</h3>
            <div class="csub">enter what you still owe &mdash; saved on this device</div>
            <div style="display:flex;flex-direction:column;gap:10px;margin-top:6px">
              <label style="display:flex;justify-content:space-between;align-items:center">Meta dues <input class="inp" id="dueMeta" type="number" min="0" step="1000"></label>
              <label style="display:flex;justify-content:space-between;align-items:center">Courier dues <input class="inp" id="dueCourier" type="number" min="0" step="1000"></label>
              <label style="display:flex;justify-content:space-between;align-items:center">Vendor dues <input class="inp" id="dueVendor" type="number" min="0" step="1000"></label>
            </div>
          </div>
        </div>
      </section>

      <!-- 7. STOCK CLEARANCE -->
      <section class="section" id="r7">
        <h2><span class="n">7</span> <span id="t7">Stock Clearance Planner</span> <span class="tag est">PROXY</span></h2>
        <div class="card ph">
          <h3>Fast-declining sellers (clearance candidates)</h3>
          <div class="csub">no inventory feed yet &mdash; showing products whose revenue dropped most vs previous 30 days, as a clearance proxy</div>
          <table id="stockTable"><thead><tr>
            <th data-k="name">Product</th><th data-k="c">Category</th>
            <th data-k="rev">Rev 30d</th><th data-k="rev0">Prev 30d</th><th data-k="drop">Drop</th>
          </tr></thead><tbody></tbody></table>
        </div>
      </section>

      <!-- 8. PRODUCT LIFE CYCLE -->
      <section class="section" id="r8">
        <h2><span class="n">8</span> <span id="t8">Product Life Cycle</span> <span class="tag est">ESTIMATED</span></h2>
        <div class="card">
          <h3>Launch &middot; Scale &middot; Maturity &middot; Downfall</h3>
          <div class="csub">from product age (first order) + 30-day revenue trend. Launch = live &le; 30d; Scale = growing &gt;15%; Maturity = steady; Downfall = falling &gt;15%.</div>
          <div class="grid4" id="lifeWrap" style="margin-top:6px"></div>
        </div>
      </section>

      <!-- 9. DAILY DECISION BOARD -->
      <section class="section" id="r9">
        <h2><span class="n">9</span> <span id="t9">Daily Decision Board</span></h2>
        <div class="card">
          <h3 id="h9t">What to do with budget today</h3>
          <div class="csub">categories sorted into actions by ROI vs your profit anchors</div>
          <div class="decision" id="decisionWrap" style="margin-top:6px"></div>
        </div>
      </section>

      <div class="footbanner">PROFIT FIRST &rarr; <b>5,000 ORDERS DAILY</b> &rarr; HEALTHY ROI (2.5&ndash;2.7) &rarr; THEN SCALE BUDGET</div>
    </div><!-- /reportWrap -->

    <!-- HOME DECOR (rich module: live from Meta, embedded at build) -->
    <div id="view-homedecor" class="wrap" style="display:none">
      <div class="dutybanner"><div class="dutytrack" id="dutyTrack"></div></div>
      <section class="section">
        <h2><span class="n" id="hdIcon">&#128302;</span> <span id="hdName">Crystal Home Decor</span> <span class="muted" id="hdScope" style="text-transform:none;letter-spacing:0;font-size:13px"></span></h2>
        <div style="display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin:8px 0 6px">
          <div class="seg" id="hdTabs"></div>
          <div class="spacer"></div>
          <span class="muted" style="font-size:12px">Range</span>
          <div class="seg" id="hdPresetSeg"></div>
          <button id="hdRefreshBtn" onclick="hdRefresh()" title="Pull fresh data from Meta + Shopify and redeploy (~10 min)" style="background:#6366f1;color:#fff;border:none;border-radius:7px;padding:7px 12px;font-size:12px;font-weight:600;cursor:pointer">&#8635; Refresh data</button>
        </div>
        <div id="hdRefreshMsg" style="font-size:12px;margin:0 0 4px"></div>
        <div class="muted" id="hdFetched" style="font-size:12px;margin-bottom:8px"></div>
      </section>
      <div id="hdBody"></div>
      <div id="hdModal" onclick="hdCloseModal(event)" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.82);z-index:9999;align-items:center;justify-content:center;flex-direction:column;gap:12px">
        <div id="hdModalContent"></div>
        <div id="hdModalCap" style="color:#fff;font-size:12px;max-width:90vw;text-align:center;opacity:.85"></div>
        <button id="hdModalX" onclick="hdCloseModal(event)" style="position:absolute;top:16px;right:20px;background:#fff;color:#111;border:none;border-radius:6px;padding:6px 12px;cursor:pointer;font-weight:600">Close &#10005;</button>
      </div>
    </div><!-- /view-homedecor -->
  </div>
</div>
<script>
const P = /*__PAYLOAD__*/;
const CATDATA = /*__CATDATA__*/;   // {category: {preset: payload}} — live from Meta, embedded
const GOAL = P.goalOrders || 5000;
const PRODS = (P.products && P.products.rows) || [];
const WEB = {SM:'Studd Muffyn', NBP:'Nuskhe By Paras', SML:'Studd Muffyn Life'};
const CAT_ICON = {'Skin':'&#129496;','Hair':'&#128088;','24K Jewellery':'&#128142;','Crystal Home Decor':'&#128302;','Crystal Accessory':'&#128255;','Nutraceuticals':'&#128138;','Perfumes':'&#127810;','Other':'&#128230;','Aibot':'&#129302;'};

const fmtINR = n => { n=Math.round(n||0); const a=Math.abs(n);
  if(a>=1e7) return '₹'+(n/1e7).toFixed(2)+'Cr';
  if(a>=1e5) return '₹'+(n/1e5).toFixed(2)+'L';
  if(a>=1e3) return '₹'+(n/1e3).toFixed(1)+'k';
  return '₹'+n; };
const fmtNum = n => Math.round(n||0).toLocaleString('en-IN');
const esc = s => (s||'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));

const LS='antk_pf_cfg';
function loadCfg(){ try{return JSON.parse(localStorage.getItem(LS))||{};}catch(e){return {};} }
function saveCfg(){ try{localStorage.setItem(LS, JSON.stringify({anchors:STATE.anchors,dues:STATE.dues}));}catch(e){} }

let STATE = {
  portal:'ALL', from:P.minDate, to:P.maxDate, view:'home', cat:null,
  anchors: Object.assign({p10:2.2,p20:2.6}, P.anchors||{}),
  dues:{meta:null, courier:0, vendor:0},
};
(function(){const c=loadCfg(); if(c.anchors)Object.assign(STATE.anchors,c.anchors); if(c.dues)Object.assign(STATE.dues,c.dues);})();

// ---------- profit model (ROI -> Profit%) ----------
function lineParams(){const a=STATE.anchors; const slope=10/((a.p20)-(a.p10)||0.4); const intercept=10-slope*a.p10; return {slope,intercept};}
function profitPct(roi){const {slope,intercept}=lineParams(); return slope*roi+intercept;}
function roiForProfit(p){const {slope,intercept}=lineParams(); return slope!==0?(p-intercept)/slope:0;}
function breakevenROI(){const {slope,intercept}=lineParams(); return slope!==0? -intercept/slope : 1.8;}
function statusOf(roi){const a=STATE.anchors, be=breakevenROI();
  if(roi>=a.p20) return {k:'scale',label:'SCALE'};
  if(roi>=a.p10) return {k:'maintain',label:'MAINTAIN'};
  if(roi>=be)    return {k:'reduce',label:'REDUCE'};
  return {k:'exit',label:'EXIT'};}
const roiClass = r => {const a=STATE.anchors; return r>=a.p20?'g':r>=a.p10?'m':'b';};
const profClass = p => p>=20?'g':p>=10?'m':'b';

// ---------- filters ----------
const inPortal=p=>STATE.portal==='ALL'||p===STATE.portal;
const inRange=d=>d>=STATE.from&&d<=STATE.to;
const adRows=()=>P.adDays.filter(r=>inPortal(r.p)&&inRange(r.d));
const shopRows=()=>P.shop.filter(r=>inPortal(r.p)&&inRange(r.d));
const catRows=()=>P.catRev.filter(r=>inPortal(r.p)&&inRange(r.d));
function nDays(){return Math.max(1, Math.round((new Date(STATE.to)-new Date(STATE.from))/864e5)+1);}
const curCat=()=>STATE.view==='category'?STATE.cat:null;

// ---------- aggregation ----------
function agg(){
  let orderRev=0,orders=0,prepaid=0,spend=0,lineRev=0;
  shopRows().forEach(r=>{orderRev+=r.rev;orders+=r.o;prepaid+=r.pp;});
  catRows().forEach(r=>{lineRev+=r.rev;});
  adRows().forEach(r=>{spend+=r.s;});
  const ratio=(lineRev>0&&orderRev>0)?orderRev/lineRev:1;
  return {orderRev,orders,prepaid,spend,lineRev,ratio,roi:spend>0?orderRev/spend:0};
}
function buildCats(){
  const A=agg(); const m={};
  adRows().forEach(r=>{(m[r.c]=m[r.c]||{spend:0,line:0,units:0,orders:0}).spend+=r.s;});
  catRows().forEach(r=>{const a=(m[r.c]=m[r.c]||{spend:0,line:0,units:0,orders:0});a.line+=r.rev;a.units+=r.u;a.orders+=r.o;});
  let rows=Object.keys(m).map(c=>{const a=m[c];const net=a.line*A.ratio;const roi=a.spend>0?net/a.spend:0;
    return {cat:c,spend:a.spend,net,line:a.line,units:a.units,orders:a.orders,roi,profit:profitPct(roi)};});
  let bySrc={sheet:0,name:0,none:0}; catRows().forEach(r=>{bySrc[r.src]=(bySrc[r.src]||0)+r.rev;});
  const tot=(bySrc.sheet+bySrc.name+bySrc.none)||1;
  return {rows,A,cover:{sheet:100*bySrc.sheet/tot,name:100*bySrc.name/tot,none:100*bySrc.none/tot}};
}
function catRoiMap(){const m={}; buildCats().rows.forEach(r=>m[r.cat]=r.roi); return m;}
// per-portal stats (range-scoped), optional category
function portalStats(p,cat){
  let orderRev=0,orders=0,spend=0,line=0,units=0;
  P.shop.forEach(r=>{if(r.p===p&&inRange(r.d)){orderRev+=r.rev;orders+=r.o;}});
  P.adDays.forEach(r=>{if(r.p===p&&inRange(r.d)&&(!cat||r.c===cat)){spend+=r.s;}});
  P.catRev.forEach(r=>{if(r.p===p&&inRange(r.d)){if(!cat||r.c===cat){line+=r.rev;units+=r.u;}}});
  // ratio reconciles this portal's line revenue to its order total
  let plAll=0; P.catRev.forEach(r=>{if(r.p===p&&inRange(r.d))plAll+=r.rev;});
  const ratio=(plAll>0&&orderRev>0)?orderRev/plAll:1;
  const net=cat?line*ratio:orderRev;
  return {orderRev,orders,spend,net,units,roi:spend>0?net/spend:0};
}

const PORTALS = P.portals.filter(p=>p!=='ALL');
const prodList = () => PRODS.filter(p=>!curCat()||p.c===curCat());

// ---------- live today (restores the original "Live KPIs" hero) ----------
function renderLive(){
  const bar=document.getElementById('liveBar');
  if(curCat()){ bar.style.display='none'; return; }   // company home only
  const last=P.maxDate, isToday=last===P.today;
  let sales=0,orders=0,spend=0;
  P.shop.forEach(r=>{if(r.d===last&&inPortal(r.p)){sales+=r.rev;orders+=r.o;}});
  P.adDays.forEach(r=>{if(r.d===last&&inPortal(r.p)){spend+=r.s;}});
  const roas=spend>0?sales/spend:0;
  const scope=(isToday?'today '+last+' (so far)':'latest day '+last)+(STATE.portal!=='ALL'?' · '+STATE.portal:'');
  bar.style.display='';
  bar.innerHTML='<div class="lh"><span class="led"></span> LIVE SALES <small>— '+scope+'</small></div>'+
    '<div class="livekpis">'+
      '<div class="lk"><div class="lkl">Sales (Shopify)</div><div class="lkv">'+fmtINR(sales)+'</div></div>'+
      '<div class="lk"><div class="lkl">Orders</div><div class="lkv">'+fmtNum(orders)+'</div></div>'+
      '<div class="lk"><div class="lkl">ROAS</div><div class="lkv"><span class="rg '+roiClass(roas)+'">'+roas.toFixed(2)+'×</span></div></div>'+
      '<div class="lk"><div class="lkl">Ad spend</div><div class="lkv">'+fmtINR(spend)+'</div></div>'+
    '</div>';
}
// ---------- 1. overview ----------
function renderOverview(){
  const cat=curCat(); const wrap=document.getElementById('ovKpis');
  let kp='';
  if(!cat){
    const A=agg(), days=nDays(), opd=A.orders/days;
    const goalPct=Math.min(100,100*opd/GOAL);
    kp+=kpi('Revenue','<span class="tag gt">SHOPIFY</span>',fmtINR(A.orderRev),'net order value, non-cancelled');
    kp+=kpi('Orders','<span class="tag gt">SHOPIFY</span>',fmtNum(A.orders),days+' days in range');
    kp+=kpi('Orders / day','',fmtNum(opd),'goal '+fmtNum(GOAL)+'/day<div class="progress"><i style="width:'+goalPct+'%"></i></div>');
    kp+=kpi('Meta spend','<span class="tag">META</span>',fmtINR(A.spend),'real ad spend, all accounts');
    kp+=kpi('Blended ROI','','<span class="rg '+roiClass(A.roi)+'">'+A.roi.toFixed(2)+'×</span>','revenue / spend');
  } else {
    const r=buildCats().rows.find(x=>x.cat===cat)||{spend:0,net:0,units:0,orders:0,roi:0,profit:0};
    const st=statusOf(r.roi);
    kp+=kpi('Revenue','<span class="tag gt">SHOPIFY</span>',fmtINR(r.net),'real Shopify, reconciled');
    kp+=kpi('Units sold','',fmtNum(r.units),'in selected range');
    kp+=kpi('Meta spend','<span class="tag">META</span>',fmtINR(r.spend),'spend tagged to this category');
    kp+=kpi('ROI','',' <span class="rg '+roiClass(r.roi)+'">'+r.roi.toFixed(2)+'×</span>','revenue / spend');
    kp+=kpi('Status','','<span class="st '+st.k+'">'+st.label+'</span>','action vs ROI goal');
  }
  wrap.innerHTML=kp;
  // website split
  const isCat=!!cat, head=['Website','Revenue',isCat?'Units':'Orders','Spend','ROI'];
  let h='<thead><tr>'+head.map((c,i)=>'<th'+(i?'':'')+'>'+c+'</th>').join('')+'</tr></thead><tbody>';
  PORTALS.forEach(p=>{const s=portalStats(p,cat); if(s.orderRev===0&&s.spend===0&&s.net===0)return;
    h+='<tr><td><span class="chip">'+p+'</span> '+(WEB[p]||'')+'</td><td>'+fmtINR(s.net)+'</td><td>'+fmtNum(isCat?s.units:s.orders)+'</td><td>'+fmtINR(s.spend)+'</td><td><span class="rg '+roiClass(s.roi)+'">'+s.roi.toFixed(2)+'×</span></td></tr>';});
  h+='</tbody>';
  document.querySelector('#ovSplit').innerHTML=h;
}
function kpi(lbl,tag,val,sub){return '<div class="kpi"><div class="lbl">'+lbl+' '+tag+'</div><div class="val">'+val+'</div><div class="sub">'+sub+'</div></div>';}

// ---------- 2. category-wise budget ----------
let catSort={k:'net',dir:-1};
function renderCatBudget(){
  const cat=curCat(); const B=buildCats();
  let rows, firstCol;
  if(!cat){ rows=B.rows.map(r=>({key:r.cat,spend:r.spend,net:r.net,units:r.units,roi:r.roi,profit:r.profit})); firstCol='Category';
    document.getElementById('h2t').textContent='Spend, revenue, ROI & profit by category';
  } else {
    firstCol='Website';
    document.getElementById('h2t').innerHTML='“'+esc(cat)+'” by website';
    rows=PORTALS.map(p=>{const s=portalStats(p,cat);return {key:p,spend:s.spend,net:s.net,units:s.units,roi:s.roi,profit:profitPct(s.roi)};}).filter(r=>r.spend||r.net);
  }
  rows.sort((a,b)=>{const k=catSort.k,va=a[k],vb=b[k];return (typeof va==='string'?String(va).localeCompare(vb):va-vb)*catSort.dir;});
  document.querySelector('#catTable thead th').textContent=firstCol;
  let h='';
  rows.forEach(r=>{const st=statusOf(r.roi); const nm=cat?('<span class="chip">'+r.key+'</span> '+(WEB[r.key]||'')):('<span class="chip">'+esc(r.key)+'</span>');
    h+='<tr><td>'+nm+'</td><td>'+fmtINR(r.spend)+'</td><td>'+fmtINR(r.net)+'</td><td>'+fmtNum(r.units)+'</td><td><span class="rg '+roiClass(r.roi)+'">'+r.roi.toFixed(2)+'×</span></td><td><span class="st '+st.k+'">'+st.label+'</span></td></tr>';});
  document.querySelector('#catTable tbody').innerHTML=h||'<tr><td colspan="6" class="muted">No data in range.</td></tr>';
  document.getElementById('coverLine').innerHTML='Revenue mapping: <b>'+B.cover.sheet.toFixed(1)+'%</b> from master SKU sheet, <b>'+B.cover.name.toFixed(1)+'%</b> name-matched (estimate), <b>'+B.cover.none.toFixed(1)+'%</b> uncategorized.';
}

// ---------- 3. budget allocation ----------
function renderAllocation(){
  const cat=curCat(); let items, totSpend=0, totRev=0;
  if(!cat){ const r=buildCats().rows; r.forEach(x=>{totSpend+=x.spend;totRev+=x.net;});
    items=r.map(x=>({nm:x.cat,sp:x.spend,rv:x.net})); }
  else { items=PORTALS.map(p=>{const s=portalStats(p,cat);return {nm:(WEB[p]||p),sp:s.spend,rv:s.net};});
    items.forEach(x=>{totSpend+=x.sp;totRev+=x.rv;}); }
  items=items.filter(x=>x.sp||x.rv).sort((a,b)=>b.rv-a.rv);
  let h='<div class="csub" style="margin:0 0 10px"><span style="color:#2f5bff">&#9632;</span> spend share &nbsp; <span style="color:#0c9b5b">&#9632;</span> revenue share</div>';
  items.forEach(x=>{const sp=totSpend>0?100*x.sp/totSpend:0, rv=totRev>0?100*x.rv/totRev:0;
    const over=sp>rv+5;
    h+='<div class="alloc-row"><div class="nm"'+(over?' style="color:var(--reduce)"':'')+'>'+esc(x.nm)+(over?' &#9650;':'')+'</div>'
      +'<div><div class="bar"><i style="width:'+sp.toFixed(1)+'%;background:#2f5bff"></i></div><div class="bar" style="margin-top:4px"><i style="width:'+rv.toFixed(1)+'%;background:#0c9b5b"></i></div></div>'
      +'<div style="text-align:right;font-size:12px">S <b>'+sp.toFixed(1)+'%</b><br>R <b>'+rv.toFixed(1)+'%</b></div></div>';});
  document.getElementById('allocWrap').innerHTML=h;
}

// ---------- 4. profit contribution ----------
function renderContribution(){
  const cm=catRoiMap(); const list=prodList().slice().sort((a,b)=>b.rev-a.rev);
  document.getElementById('prodWin').textContent='last 30 days ('+(P.products.curFrom||'')+' → '+(P.products.curTo||'')+') · gross product revenue';
  let h='';
  list.slice(0,10).forEach((p,i)=>{h+='<div class="rank"><div class="num">'+(i+1)+'</div><div class="nm" title="'+esc(p.name)+'">'+esc(p.name)+'</div><div class="mt">'+fmtINR(p.rev)+'</div></div>';});
  document.getElementById('rankWrap').innerHTML=h||'<div class="muted">No products.</div>';
  // contribution by est profit
  const prof=list.map(p=>({p,profit:p.rev*profitPct(cm[p.c]!=null?cm[p.c]:agg().roi)/100}));
  const totPos=prof.reduce((s,x)=>s+Math.max(0,x.profit),0)||1;
  let c='';
  prof.sort((a,b)=>b.profit-a.profit).slice(0,10).forEach(x=>{const sh=100*Math.max(0,x.profit)/totPos;
    c+='<div class="rank"><div class="nm" title="'+esc(x.p.name)+'">'+esc(x.p.name)+'</div><div class="mt"><span class="rg '+profClass(x.profit>=0?20:-1)+'">'+fmtINR(x.profit)+'</span> · '+sh.toFixed(1)+'%</div></div>';});
  document.getElementById('contribWrap').innerHTML=c||'<div class="muted">No products.</div>';
}

// ---------- 5. profitability ----------
let profSort={k:'rev',dir:-1};
function renderProfitability(){
  const cm=catRoiMap(); let rows=prodList().map(p=>{const roi=cm[p.c]!=null?cm[p.c]:0; const trend=p.rev0>0?p.rev/p.rev0-1:(p.rev>0?1:0);
    return {name:p.name,c:p.c,rev:p.rev,trend,roi,status:statusOf(roi).k};});
  rows.sort((a,b)=>{const k=profSort.k,va=a[k],vb=b[k];return (typeof va==='string'?String(va).localeCompare(vb):va-vb)*profSort.dir;});
  let h='';
  rows.slice(0,40).forEach(r=>{const st=statusOf(r.roi); const tc=r.trend>=0?'g':'b'; const ar=r.trend>=0?'&#9650;':'&#9660;';
    h+='<tr><td title="'+esc(r.name)+'" style="max-width:260px;overflow:hidden;text-overflow:ellipsis">'+esc(r.name)+'</td><td><span class="chip">'+esc(r.c)+'</span></td><td>'+fmtINR(r.rev)+'</td><td><span class="rg '+tc+'">'+ar+' '+Math.abs(r.trend*100).toFixed(0)+'%</span></td><td><span class="rg '+roiClass(r.roi)+'">'+r.roi.toFixed(2)+'×</span></td><td><span class="st '+st.k+'">'+st.label+'</span></td></tr>';});
  document.querySelector('#profitTable tbody').innerHTML=h||'<tr><td colspan="6" class="muted">No products.</td></tr>';
}

// ---------- date helper ----------
function isoAdd(d,delta){const t=new Date(d+'T00:00:00');t.setDate(t.getDate()+delta);return t.toISOString().slice(0,10);}

// ---------- 6. budget in hand ----------
function renderBudgetInHand(){
  const today=P.maxDate;
  let todayRev=0,todaySpend=0;
  P.shop.forEach(r=>{if(r.d===today&&inPortal(r.p))todayRev+=r.rev;});
  P.adDays.forEach(r=>{if(r.d===today&&inPortal(r.p))todaySpend+=r.s;});
  // 7-day rolling ROI (partial-day single-day ROI is unreliable, per ops rule)
  const s7=isoAdd(today,-6); let rev7=0,sp7=0;
  P.shop.forEach(r=>{if(r.d>=s7&&r.d<=today&&inPortal(r.p))rev7+=r.rev;});
  P.adDays.forEach(r=>{if(r.d>=s7&&r.d<=today&&inPortal(r.p))sp7+=r.s;});
  const roi7=sp7>0?rev7/sp7:0; const pp=profitPct(roi7);
  const todayProfit=todayRev*pp/100;
  if(STATE.dues.meta==null) STATE.dues.meta=Math.round(todaySpend);
  document.getElementById('dueMeta').value=STATE.dues.meta;
  document.getElementById('dueCourier').value=STATE.dues.courier;
  document.getElementById('dueVendor').value=STATE.dues.vendor;
  const meta=+STATE.dues.meta||0, courier=+STATE.dues.courier||0, vendor=+STATE.dues.vendor||0;
  const bih=todayProfit-meta-courier-vendor;
  document.getElementById('scope6').textContent=' — today '+today+(STATE.portal!=='ALL'?' · '+STATE.portal:'');
  let h='<tbody>'
    +row('Today revenue (Shopify)',fmtINR(todayRev))
    +row('Today profit (est)','<b>'+fmtINR(todayProfit)+'</b>')
    +row('− Meta dues','-'+fmtINR(meta))
    +row('− Courier dues','-'+fmtINR(courier))
    +row('− Vendor dues','-'+fmtINR(vendor))
    +'</tbody>';
  document.getElementById('bihTable').innerHTML=h;
  document.getElementById('bihVal').innerHTML='Budget in hand: <span class="rg '+(bih>=0?'g':'b')+'">'+fmtINR(bih)+'</span>';
  document.getElementById('bihNote').textContent=bih>=0?'free cash to redeploy into scaling':'shortfall — hold scaling until dues clear';
  function row(a,b){return '<tr><td>'+a+'</td><td>'+b+'</td></tr>';}
}

// ---------- 7. stock clearance (proxy) ----------
function renderStock(){
  let rows=prodList().filter(p=>p.rev0>0&&p.rev<p.rev0)
    .map(p=>({name:p.name,c:p.c,rev:p.rev,rev0:p.rev0,drop:(p.rev/p.rev0-1)*100}))
    .sort((a,b)=>(a.rev-a.rev0)-(b.rev-b.rev0));
  let h='';
  rows.slice(0,10).forEach(r=>{h+='<tr><td title="'+esc(r.name)+'" style="max-width:260px;overflow:hidden;text-overflow:ellipsis">'+esc(r.name)+'</td><td><span class="chip">'+esc(r.c)+'</span></td><td>'+fmtINR(r.rev)+'</td><td>'+fmtINR(r.rev0)+'</td><td><span class="rg b">'+r.drop.toFixed(0)+'%</span></td></tr>';});
  document.querySelector('#stockTable tbody').innerHTML=h||'<tr><td colspan="5" class="muted">No declining products in the last 30 days.</td></tr>';
}

// ---------- 8. product life cycle ----------
function renderLifecycle(){
  const buckets={Launch:[],Scale:[],Maturity:[],Downfall:[]};
  prodList().forEach(p=>{let b;
    if(p.live!=null&&p.live<=30) b='Launch';
    else {const t=p.rev0>0?p.rev/p.rev0:(p.rev>0?2:0); b=t>=1.15?'Scale':t>=0.85?'Maturity':'Downfall';}
    buckets[b].push(p);});
  const meta={Launch:['#2f6bff','newly live (≤30d)'],Scale:['#0c9b5b','growing >15%'],Maturity:['#6b7686','steady'],Downfall:['#d23b3b','falling >15%']};
  let h='';
  Object.keys(buckets).forEach(k=>{const arr=buckets[k].sort((a,b)=>b.rev-a.rev); const [col,desc]=meta[k];
    let chips=arr.slice(0,4).map(p=>'<div style="font-size:11.5px;color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="'+esc(p.name)+'">'+esc(p.name)+'</div>').join('');
    h+='<div class="card" style="box-shadow:none;border-radius:10px"><div class="lbl muted" style="font-weight:700;color:'+col+'">'+k+'</div><div style="font-size:24px;font-weight:780;margin:4px 0">'+arr.length+'</div><div class="muted" style="font-size:11px;margin-bottom:8px">'+desc+'</div>'+chips+'</div>';});
  document.getElementById('lifeWrap').innerHTML=h;
}

// ---------- 9. decision board ----------
function renderDecisionBoard(){
  const cat=curCat(); let rows;
  if(!cat){ rows=buildCats().rows.map(r=>({nm:r.cat,spend:r.spend,roi:r.roi})); document.getElementById('h9t').textContent='What to do with budget today'; }
  else { rows=PORTALS.map(p=>{const s=portalStats(p,cat);return {nm:(WEB[p]||p),spend:s.spend,roi:s.roi};}).filter(r=>r.spend||r.roi); document.getElementById('h9t').innerHTML='“'+esc(cat)+'” — action by website'; }
  const cols={scale:[],maintain:[],reduce:[],exit:[]};
  rows.forEach(r=>{cols[statusOf(r.roi).k].push(r);});
  const def={scale:['SCALE','var(--scale)','put more budget here'],maintain:['MAINTAIN','var(--maintain)','hold budget'],reduce:['REDUCE','var(--reduce)','trim / fix ROI'],exit:['EXIT','var(--exit)','pause / cut']};
  let h='';
  Object.keys(def).forEach(k=>{const [lbl,col,desc]=def[k]; const arr=cols[k].sort((a,b)=>b.spend-a.spend);
    let items=arr.map(r=>'<div class="di"><span>'+esc(r.nm)+'</span><span><span class="rg '+roiClass(r.roi)+'">'+r.roi.toFixed(2)+'×</span> · '+fmtINR(r.spend)+'</span></div>').join('')||'<div class="muted" style="font-size:12px">—</div>';
    h+='<div class="dcol"><h4><span class="dot" style="background:'+col+'"></span>'+lbl+'</h4><div class="muted" style="font-size:11px;margin-bottom:6px">'+desc+'</div>'+items+'</div>';});
  document.getElementById('decisionWrap').innerHTML=h;
}

// ---------- banner + chrome ----------
function renderBanner(){
  const a=STATE.anchors, be=breakevenROI();
  document.getElementById('bannerRoi').innerHTML=
    '<span>Break-even ≈ '+be.toFixed(2)+'×</span><span>10% profit @ '+a.p10.toFixed(2)+'×</span><span>20% profit @ '+a.p20.toFixed(2)+'×</span>';
}
function renderChrome(){
  document.getElementById('rangepill').textContent='Range: '+STATE.from+' → '+STATE.to;
  const maxD=PAYLOAD_MAX();
  const ageDays=Math.floor((Date.now()-new Date(maxD+'T23:59:59+05:30'))/864e5);
  const fp=document.getElementById('freshpill');
  fp.className='pill'+(ageDays>=2?' red':ageDays>=1?' amber':'');
  document.getElementById('freshtxt').textContent=ageDays<=0?'data current ('+maxD+')':'data '+ageDays+'d old (latest '+maxD+')';
  const n=document.getElementById('stalenote');
  if(ageDays>=2){n.style.display='';n.innerHTML='⚠️ Latest data is <b>'+maxD+'</b> ('+ageDays+' days old). Numbers are real, just not today — the ingest may be lagging.';}
  else n.style.display='none';
}
function PAYLOAD_MAX(){return P.maxDate;}

// ---------- view switching ----------
function setTitles(){
  const cat=curCat();
  document.getElementById('t1').textContent=cat?esc(cat)+' Overview':'Company Overview';
  document.getElementById('scope1').textContent=' — '+(STATE.portal==='ALL'?'all websites':STATE.portal)+' · '+STATE.from+' → '+STATE.to;
  document.getElementById('crumb').innerHTML=cat
    ?esc(cat)+'<small>category deep-dive · profit-first</small>'
    :'Company<small>profit-first view, all categories</small>';
}
// ===================== HOME DECOR MODULE (embedded Meta payload) =====================
let HDPRESET='yesterday', HDTAB='overview', HDCAT='Crystal Home Decor', HDPUSHSEL=null;
const roasClass = roiClass;
const HD_PRESET_LABELS={today:'Today',yesterday:'Yesterday',last_7d:'Last 7D',last_30d:'Last 30D'};
const HD_TABS=[['overview','Category Overview'],['campaigns','Campaigns'],['budget','Product-Wise Budget'],['products','All Products'],['plan','Creative Plan'],['creative','Creative Report'],['clearance','Stock Clearance'],['profit','Product Profitability'],['closed','Closed Budget']];
function hdEsc(s){return (s==null?'':String(s)).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
function rg(r){r=r||0;return '<span class="rg '+roasClass(r)+'">'+r.toFixed(2)+'×</span>';}
function hdAgo(ts){let s=Math.floor(Date.now()/1000-ts);if(s<0)s=0;if(s<60)return s+'s ago';if(s<3600)return Math.floor(s/60)+'m ago';if(s<86400)return Math.floor(s/3600)+'h ago';return Math.floor(s/86400)+'d ago';}
function hdCatData(){return (CATDATA&&CATDATA[HDCAT])||null;}
function hdHasCat(cat){return !!(CATDATA&&CATDATA[cat]);}
function hdPick(){const c=hdCatData(); return c&&(c[HDPRESET]||c.yesterday||c[Object.keys(c)[0]])||null;}
function hdKpi(l,v,sub,tag){return '<div class="kpi"><div class="lbl">'+l+(tag?' <span class="tag">'+tag+'</span>':'')+'</div><div class="val">'+v+'</div><div class="sub">'+(sub||'')+'</div></div>';}
function hdCard(ad){
  const clickable = ad.preview || ad.image || ad.thumbnail;
  const hint = ad.preview ? 'Play creative (opens Meta ad preview)' : (ad.image?'View creative':'');
  const media='<div onclick="hdOpen(this)" data-pv="'+hdEsc(ad.preview||'')+'" data-img="'+hdEsc(ad.image||ad.thumbnail||'')+'" data-name="'+hdEsc(ad.name)+'" title="'+hint+'" style="position:relative;width:120px;height:120px;background:var(--bg2,#f0f2f7);border:1px solid var(--line,#e6e9f2);border-radius:8px;overflow:hidden;display:flex;align-items:center;justify-content:center;'+(clickable?'cursor:pointer':'')+'">'+
    (ad.thumbnail?'<img src="'+ad.thumbnail+'" loading="lazy" referrerpolicy="no-referrer" style="max-width:100%;max-height:100%">':'<span>no preview</span>')+
    (ad.preview?'<span style="position:absolute;inset:0;display:flex;align-items:center;justify-content:center"><span style="width:34px;height:34px;border-radius:50%;background:rgba(0,0,0,.55);color:#fff;display:flex;align-items:center;justify-content:center;font-size:14px">&#9654;</span></span>':'')+
    '</div>';
  return '<div style="width:120px;font-size:10px" class="muted">'+media+
    '<div style="margin-top:4px;display:flex;justify-content:space-between;align-items:center"><span class="chip">'+hdEsc(ad.type||'—')+'</span>'+rg(ad.roas)+'</div>'+
    '<div style="margin-top:3px;color:var(--ink,#1f2937);overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="'+hdEsc(ad.name)+'">'+hdEsc(ad.name)+'</div>'+
    '<div style="margin-top:2px">'+fmtINR(ad.spend)+' &middot; '+fmtNum(ad.purchases)+' ord</div></div>';
}
function hdOpen(el){
  const pv=el.getAttribute('data-pv'), img=el.getAttribute('data-img'), nm=el.getAttribute('data-name');
  if(pv){ window.open(pv,'_blank','noopener'); return; }   // plays the rendered ad on Meta
  const m=document.getElementById('hdModal'), c=document.getElementById('hdModalContent');
  if(img){ c.innerHTML='<img src="'+img+'" referrerpolicy="no-referrer" style="max-width:92vw;max-height:80vh;border-radius:10px">'; }
  else { c.innerHTML='<div style="color:#fff">No preview available for this ad.</div>'; }
  document.getElementById('hdModalCap').textContent=nm||'';
  m.style.display='flex';
}
function hdCloseModal(ev){
  if(ev && ev.target && ev.target.id!=='hdModal' && ev.target.id!=='hdModalX') return;
  document.getElementById('hdModal').style.display='none';
  document.getElementById('hdModalContent').innerHTML='';
}
function hdFmtCard(l,o){o=o||{};return '<div class="card" style="box-shadow:none;border-radius:10px"><div class="lbl muted" style="font-size:12px;font-weight:600">'+l+'</div><div style="font-size:20px;font-weight:750;margin:4px 0">'+fmtNum(o.count||0)+' ads</div><div style="font-size:12px" class="muted">'+fmtINR(o.spend||0)+' &middot; '+rg(o.roas)+'</div></div>';}
function hdOverview(d){
  const o=d.overview||{};
  const net=(o.revenue||0)-(o.spend||0);
  let h='<section class="section"><div class="kpis" style="grid-template-columns:repeat(5,1fr)">';
  h+=hdKpi('Running Budget',fmtINR(o.budget),'effective ₹/day');
  h+=hdKpi('Spend',fmtINR(o.spend),(d.since||'')+' → '+(d.until||''),'META');
  h+=hdKpi('Revenue',fmtINR(o.revenue),'vs '+fmtINR(o.spend)+' spend &middot; '+(net>=0?'+':'')+fmtINR(net)+' net','PIXEL');
  h+=hdKpi('ROAS',rg(o.roas),'revenue / spend','PIXEL');
  h+=hdKpi('Orders',fmtNum(o.orders),fmtINR(o.revenue)+' revenue','PIXEL');
  h+='</div></section>';
  h+=hdPrepaid(d);
  h+='<section class="section"><div class="card"><h3>Website-wise overview</h3><table><thead><tr><th>Website</th><th>Budget/day</th><th>Spend</th><th>Revenue</th><th>ROAS</th><th>Orders</th><th>Campaigns</th></tr></thead><tbody>';
  (d.websites||[]).forEach(w=>{h+='<tr><td><span class="chip">'+hdEsc(w.name)+'</span></td><td>'+fmtINR(w.budget)+'</td><td>'+fmtINR(w.spend)+'</td><td>'+fmtINR(w.revenue)+'</td><td>'+rg(w.roas)+'</td><td>'+fmtNum(w.orders)+'</td><td>'+fmtNum(w.campaigns)+'</td></tr>';});
  h+='</tbody></table></div></section>';
  h+='<section class="section"><div class="card"><h3>Products snapshot</h3><table><thead><tr><th>Product</th><th>Budget/day</th><th>Spend</th><th>Revenue</th><th>ROAS</th><th>Orders</th><th>Campaigns</th></tr></thead><tbody>';
  (d.products||[]).forEach(p=>{h+='<tr><td><b>'+hdEsc(p.product)+'</b></td><td>'+fmtINR(p.budget)+'</td><td>'+fmtINR(p.spend)+'</td><td>'+fmtINR(p.revenue)+'</td><td>'+rg(p.roas)+'</td><td>'+fmtNum(p.orders)+'</td><td>'+fmtNum(p.campaigns)+'</td></tr>';});
  h+='</tbody></table></div></section>';
  return h;
}
function hdPrepaid(d){
  const portals=(d.websites||[]).filter(w=>w.campaigns>0).map(w=>w.portal);
  const names=(d.websites||[]).filter(w=>w.campaigns>0).map(w=>w.name).join(', ')||'—';
  let o=0,pp=0,cod=0;
  const shop=(typeof PAYLOAD!=='undefined' && PAYLOAD.shop)?PAYLOAD.shop:[];
  shop.forEach(r=>{ if(portals.indexOf(r.p)>-1 && r.d>=(d.since||'') && r.d<=(d.until||'')){ o+=(r.o||0); pp+=(r.pp||0); cod+=(r.cod!=null?r.cod:((r.o||0)-(r.pp||0))); } });
  const pct=x=> o>0?(100*x/o).toFixed(1)+'%':'—';
  let h='<section class="section"><div class="card"><h3>Payment split &mdash; Prepaid vs COD</h3>'+
    '<div class="csub">Shopify store orders for '+hdEsc(names)+' &middot; '+(d.since||'')+' → '+(d.until||'')+' &middot; store-level (Shopify orders are not split by category)</div>'+
    '<div class="kpis" style="grid-template-columns:repeat(3,1fr);margin-top:8px">';
  h+=hdKpi('Store orders',fmtNum(o),'Shopify','SHOPIFY');
  h+=hdKpi('Prepaid',fmtNum(pp)+' <span class="muted" style="font-size:13px">('+pct(pp)+')</span>','paid online');
  h+=hdKpi('COD',fmtNum(cod)+' <span class="muted" style="font-size:13px">('+pct(cod)+')</span>','cash on delivery');
  h+='</div></div></section>';
  return h;
}
async function hdRefresh(){
  const b=document.getElementById('hdRefreshBtn'), msg=document.getElementById('hdRefreshMsg');
  const old=b.innerHTML; b.disabled=true; b.innerHTML='Refreshing…';
  try{
    const r=await fetch('/api/refresh',{method:'POST'});
    const j=await r.json().catch(()=>({}));
    if(r.ok && j.ok){ msg.style.color='#1a9d52'; msg.textContent='✓ Refresh started — fresh data deploys in ~10 min. Reload the page after that.'; }
    else { msg.style.color='#d4434a'; msg.textContent='Refresh unavailable'+(j.error?' ('+j.error+')':' (HTTP '+r.status+')')+' — admin must set GH_DISPATCH_TOKEN in Vercel env.'; }
  }catch(e){ msg.style.color='#d4434a'; msg.textContent='Refresh failed: '+e.message; }
  b.disabled=false; b.innerHTML=old;
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
    h+='<details style="border-bottom:1px solid var(--line,#e6e9f2);padding:7px 0"><summary style="cursor:pointer"><b>'+hdEsc(c.name)+'</b> <span class="muted" style="font-family:monospace;font-size:11px">'+c.id+'</span><br><span class="muted" style="font-size:12px">'+c.adset_count+' ad sets &middot; '+fmtINR(c.budget)+'/day &middot; '+fmtINR(c.spend)+' spend &middot; '+rg(c.roas)+' &middot; '+fmtNum(c.orders)+' orders'+(c.days_running!=null?' &middot; running '+fmtNum(c.days_running)+'d':'')+'</span></summary><div style="padding:8px 0 4px 14px">';
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
// ---------- All Products (editable class + target budget, saved on device) ----------
function hdCfgLoad(){try{return JSON.parse(localStorage.getItem('hdAllProducts')||'{}')||{};}catch(e){return {};}}
function hdCfgSave(c){try{localStorage.setItem('hdAllProducts',JSON.stringify(c));}catch(e){}}
function hdKey(prod){return HDCAT+'::'+prod;}
function hdClassLabel(k){return k==='new'?'New':k==='clearance'?'Stock Clearance':'Old';}
function hdStage(p){const r=p.roas||0,d=p.days_running;
  if(d!=null&&d<=30) return ['Launch','#2f6bff'];
  if(r>=2.2) return ['Scale','#0c9b5b'];
  if(r>=1.8) return ['Maturity','#6b7686'];
  return ['Downfall','#d23b3b'];}
function hdDefaultClass(p){const r=p.roas||0,d=p.days_running;
  if(d!=null&&d<=30) return 'new';
  if(r<1.8&&(p.spend||0)>0) return 'clearance';
  return 'old';}
function hdSetClass(el){const c=hdCfgLoad(),k=el.getAttribute('data-k');(c[k]=c[k]||{}).cls=el.value;hdCfgSave(c);hdRender();}
function hdSetTarget(el){const c=hdCfgLoad(),k=el.getAttribute('data-k');const v=el.value.trim();
  c[k]=c[k]||{}; if(v==='')delete c[k].target; else c[k].target=+v||0; hdCfgSave(c);hdRender();}
function hdAllProducts(d){
  const cfg=hdCfgLoad();
  const ps=(d.products||[]).slice().sort((a,b)=>(b.spend||0)-(a.spend||0));
  const t20=roiForProfit(20);
  const buckets={new:{cnt:0,bud:0,tgt:0},old:{cnt:0,bud:0,tgt:0},clearance:{cnt:0,bud:0,tgt:0}};
  const rowsData=ps.map(p=>{const k=hdKey(p.product),saved=cfg[k]||{};
    const cls=saved.cls||hdDefaultClass(p);
    const tgt=(saved.target!=null)?saved.target:'';
    const b=buckets[cls]||buckets.old; b.cnt++; b.bud+=(p.budget||0); b.tgt+=(+tgt||0);
    return {p,cls,tgt,k};});
  let h='<section class="section"><div class="kpis" style="grid-template-columns:repeat(3,1fr)">';
  ['new','old','clearance'].forEach(k=>{const b=buckets[k];
    h+=hdKpi('Target — '+hdClassLabel(k),fmtINR(b.tgt),fmtNum(b.cnt)+' products · now '+fmtINR(b.bud)+'/day');});
  h+='</div></section>';
  h+='<section class="section"><div class="card"><h3>All products — stage, class &amp; target budget</h3>'+
     '<div class="csub">Tag each product New / Old / Stock Clearance and set a target daily budget. Saved on this device. Maturity stage is estimated from age + ROAS.</div>'+
     '<table><thead><tr><th>Product</th><th>Maturity stage</th><th>Class</th><th>Spend</th><th>Profit ratio</th><th title="ROAS needed for 20% profit">20% ROAS</th><th>Budget/day now</th><th>Target budget/day</th></tr></thead><tbody>';
  rowsData.forEach(r=>{const p=r.p,st=hdStage(p);
    const opt=(v,l)=>'<option value="'+v+'"'+(r.cls===v?' selected':'')+'>'+l+'</option>';
    h+='<tr><td><b>'+hdEsc(p.product)+'</b>'+(p.days_running!=null?' <span class="muted" style="font-size:11px">'+fmtNum(p.days_running)+'d</span>':'')+'</td>'+
      '<td><span style="color:'+st[1]+';font-weight:600">'+st[0]+'</span></td>'+
      '<td><select data-k="'+hdEsc(r.k)+'" onchange="hdSetClass(this)" style="font-size:12px;padding:3px 6px;border:1px solid var(--line,#e6e9f2);border-radius:6px;background:var(--card,#fff);color:inherit">'+
        opt('new','New')+opt('old','Old')+opt('clearance','Stock Clearance')+'</select></td>'+
      '<td>'+fmtINR(p.spend)+'</td>'+
      '<td>'+rg(p.roas)+'</td>'+
      '<td class="muted">'+t20.toFixed(2)+'×</td>'+
      '<td>'+fmtINR(p.budget)+'</td>'+
      '<td><input type="number" min="0" step="100" value="'+(r.tgt===''?'':r.tgt)+'" data-k="'+hdEsc(r.k)+'" onchange="hdSetTarget(this)" placeholder="set" style="width:110px;font-size:12px;padding:4px 6px;border:1px solid var(--line,#e6e9f2);border-radius:6px;background:var(--card,#fff);color:inherit"></td></tr>';});
  if(!ps.length) h+='<tr><td colspan="8" class="muted">No active products in this category.</td></tr>';
  h+='</tbody></table></div></section>';
  return h;
}
// ---------- Creative Plan: requirement chart + push calendar ----------
const CRE_GROWTH=7500, CRE_MAINT=15000;
function hdPlanLoad(){try{return JSON.parse(localStorage.getItem('hdCreativePlan')||'{}')||{};}catch(e){return {};}}
function hdPlanSave(c){try{localStorage.setItem('hdCreativePlan',JSON.stringify(c));}catch(e){}}
function hdPushLoad(){try{return JSON.parse(localStorage.getItem('hdPushPlan')||'{}')||{};}catch(e){return {};}}
function hdPushSave(c){try{localStorage.setItem('hdPushPlan',JSON.stringify(c));}catch(e){}}
function hdCreativeReq(cur,tgt){const g=Math.max(0,(tgt||0)-(cur||0))/CRE_GROWTH, m=(cur||0)/CRE_MAINT; return {g:g,m:m,total:Math.ceil(g+m)};}
function hdBucketClasses(d){const cfg=hdCfgLoad(),b={new:[],old:[],clearance:[]};
  (d.products||[]).forEach(p=>{const k=hdKey(p.product),cls=(cfg[k]&&cfg[k].cls)||hdDefaultClass(p);(b[cls]||b.old).push(p);});
  return b;}
function hdPlanSetTarget(el){const c=hdPlanLoad(),k=HDCAT+'::'+el.getAttribute('data-cls'),v=el.value.trim();
  c[k]=c[k]||{}; if(v==='')delete c[k].target; else c[k].target=+v||0; hdPlanSave(c); hdRender();}
function hdSign(n){return (n>0?'+':n<0?'&minus;':'')+fmtINR(Math.abs(n));}
function hdPlan(d){
  const planCfg=hdPlanLoad(), b=hdBucketClasses(d);
  const meta=[['new','New products','Growth','#2f6bff'],['old','Old products','Maintenance','#0c9b5b'],['clearance','Stock clearance','Clearance','#d23b3b']];
  let h='<section class="section">';
  h+='<div class="fbox">&#129513; <b>Creatives needed = (Target &minus; Current) &divide; ₹7,500</b> (growth) <b>+ Current &divide; ₹15,000</b> (maintenance). '+
     'Current budget is pulled live from Meta; set a Target and the requirement updates instantly.</div>';
  h+='<div class="crq">';
  let tot={cur:0,tgt:0,cre:0};
  meta.forEach(m=>{const arr=b[m[0]]||[];
    const cur=arr.reduce((s,p)=>s+(p.budget||0),0);
    const k=HDCAT+'::'+m[0], saved=planCfg[k]||{};
    const tgt=(saved.target!=null)?saved.target:cur;
    const chg=tgt-cur, req=hdCreativeReq(cur,tgt);
    tot.cur+=cur; tot.tgt+=tgt; tot.cre+=req.total;
    h+='<div class="cb"><h4><span style="color:'+m[3]+'">&#9679;</span> '+m[1]+'</h4><div class="goal">'+m[2]+' &middot; '+arr.length+' products</div>'+
       '<div class="row"><span class="k">Current (Meta)</span><span>'+fmtINR(cur)+'/day</span></div>'+
       '<div class="row"><span class="k">Target /day</span><span><input class="tin" type="number" min="0" step="500" value="'+((saved.target!=null)?saved.target:'')+'" placeholder="'+Math.round(cur)+'" data-cls="'+m[0]+'" onchange="hdPlanSetTarget(this)"></span></div>'+
       '<div class="row"><span class="k">Change tomorrow</span><span style="font-weight:700;color:'+(chg>0?'var(--good)':chg<0?'var(--bad)':'var(--muted)')+'">'+hdSign(chg)+'</span></div>'+
       '<div class="cr"><div class="n">'+req.total+'</div><div class="l">creatives needed<br>(<span style="color:#2f6bff">'+req.g.toFixed(1)+' growth</span> + <span style="color:#0c9b5b">'+req.m.toFixed(1)+' maint</span>)</div></div>'+
       '</div>';});
  h+='</div>';
  h+='<div class="muted" style="font-size:12px;margin-top:10px">Category total &mdash; current '+fmtINR(tot.cur)+'/day &middot; target '+fmtINR(tot.tgt)+'/day &middot; <b style="color:var(--ink)">'+tot.cre+'</b> creatives to brief.</div>';
  h+='</section>';
  h+=hdPushCal(d);
  return h;
}
function hdPushCal(d){
  const all=hdPushLoad(), cat=all[HDCAT]||{};
  const dates=Object.keys(cat).filter(dt=>(cat[dt]||[]).length).sort();
  const today=P.today, next=dates.find(dt=>dt>=today)||dates[dates.length-1]||null;
  let sel=HDPUSHSEL; if(!sel||!cat[sel]) sel=next;
  const prodOpts=(d.products||[]).map(p=>'<option value="'+hdEsc(p.product)+'">'+hdEsc(p.product)+'</option>').join('');
  const inp='font-size:13px;padding:5px 8px;border:1px solid var(--line);border-radius:7px;background:var(--panel);color:inherit';
  let h='<section class="section"><div class="card"><h3>&#128197; Push Calendar</h3>'+
        '<div class="csub">Plan the next product push for this category &mdash; pick a date, add the products and their push budget. Saved on this device.</div>';
  h+='<div style="display:flex;gap:18px;align-items:center;flex-wrap:wrap;margin-bottom:12px">'+
     '<div><div class="muted" style="font-size:11px">Next push</div><div style="font-size:19px;font-weight:800">'+(next||'&mdash;')+'</div></div>';
  if(dates.length) h+='<div><div class="muted" style="font-size:11px">View date</div><select onchange="hdPushSelDate(this)" style="'+inp+'">'+dates.map(dt=>'<option value="'+dt+'"'+(dt===sel?' selected':'')+'>'+dt+(dt===next?' (next)':'')+'</option>').join('')+'</select></div>';
  h+='</div>';
  h+='<table><thead><tr><th style="text-align:left">Product</th><th>Push budget/day</th><th></th></tr></thead><tbody>';
  const list=(sel&&cat[sel])||[];
  if(list.length) list.forEach((e,i)=>{h+='<tr><td style="text-align:left">'+hdEsc(e.p)+'</td><td>'+fmtINR(e.b)+'</td><td><button onclick="hdPushDel(\''+sel+'\','+i+')" title="Remove" style="background:none;border:0;color:var(--bad);cursor:pointer;font-size:13px">&#10005;</button></td></tr>';});
  else h+='<tr><td colspan="3" class="muted">No products scheduled'+(sel?(' for '+sel):'')+'.</td></tr>';
  h+='</tbody></table>';
  h+='<div style="display:flex;gap:10px;align-items:flex-end;flex-wrap:wrap;margin-top:14px;padding-top:12px;border-top:1px dashed var(--line)">'+
     '<div><div class="muted" style="font-size:11px">Product</div><select id="hdPushProd" style="'+inp+';max-width:210px">'+prodOpts+'</select></div>'+
     '<div><div class="muted" style="font-size:11px">Push budget/day</div><input id="hdPushBudget" type="number" min="0" step="500" placeholder="e.g. 5000" style="'+inp+';width:130px"></div>'+
     '<div><div class="muted" style="font-size:11px">Date</div><input id="hdPushDate" type="date" value="'+(sel||today)+'" style="'+inp+'"></div>'+
     '<button onclick="hdPushAdd()" style="background:#6366f1;color:#fff;border:none;border-radius:7px;padding:8px 16px;font-size:13px;font-weight:600;cursor:pointer">+ Add</button>'+
     '</div></div></section>';
  return h;
}
function hdPushSelDate(el){HDPUSHSEL=el.value;hdRender();}
function hdPushAdd(){const prod=document.getElementById('hdPushProd'),bud=document.getElementById('hdPushBudget'),dt=document.getElementById('hdPushDate');
  const p=prod?prod.value:'',b=+((bud&&bud.value)||0),date=dt?dt.value:'';
  if(!p||!date||!(b>0)){alert('Pick a product, a budget and a date.');return;}
  const all=hdPushLoad(); all[HDCAT]=all[HDCAT]||{}; all[HDCAT][date]=all[HDCAT][date]||[]; all[HDCAT][date].push({p:p,b:b});
  hdPushSave(all); HDPUSHSEL=date; hdRender();}
function hdPushDel(date,i){const all=hdPushLoad(); if(all[HDCAT]&&all[HDCAT][date]){all[HDCAT][date].splice(i,1); if(!all[HDCAT][date].length)delete all[HDCAT][date];} hdPushSave(all); hdRender();}
function hdProfit(d){
  const ps=(d.products||[]).map(p=>{const profit=(p.revenue||0)-(p.spend||0);return Object.assign({},p,{profit:profit});}).sort((a,b)=>b.profit-a.profit);
  const tRev=ps.reduce((s,p)=>s+(p.revenue||0),0), tSpend=ps.reduce((s,p)=>s+(p.spend||0),0), tProfit=tRev-tSpend;
  const pc=v=>'<span style="color:'+(v>=0?'#1a9d52':'#d4434a')+';font-weight:700">'+fmtINR(v)+'</span>';
  let h='<section class="section"><div class="kpis" style="grid-template-columns:repeat(3,1fr)">';
  h+=hdKpi('Revenue',fmtINR(tRev),'pixel-attributed','PIXEL');
  h+=hdKpi('Ad Spend',fmtINR(tSpend),'Meta','META');
  h+=hdKpi('Ad Contribution',pc(tProfit),'revenue − ad spend');
  h+='</div></section>';
  h+='<section class="section"><div class="card"><h3>Product profitability</h3><div class="csub">Ad contribution = Pixel Revenue &minus; Ad Spend. Excludes COGS / product cost &mdash; add a cost sheet for true net profit.</div><table><thead><tr><th>Product</th><th>Spend</th><th>Revenue</th><th>Ad Contribution</th><th>ROAS</th><th>Orders</th></tr></thead><tbody>';
  ps.forEach(p=>{h+='<tr><td><b>'+hdEsc(p.product)+'</b></td><td>'+fmtINR(p.spend)+'</td><td>'+fmtINR(p.revenue)+'</td><td>'+pc(p.profit)+'</td><td>'+rg(p.roas)+'</td><td>'+fmtNum(p.orders)+'</td></tr>';});
  h+='</tbody></table></div></section>';
  return h;
}
function hdClosed(d){
  const cl=(d.closed||[]).slice().sort((a,b)=>(b.closed_on||'').localeCompare(a.closed_on||'')||(b.budget||0)-(a.budget||0));
  const freed=cl.reduce((s,c)=>s+(c.budget||0),0), spent=cl.reduce((s,c)=>s+(c.spend||0),0);
  let h='<section class="section"><div class="kpis" style="grid-template-columns:repeat(3,1fr)">';
  h+=hdKpi('Closed campaigns',fmtNum(cl.length),'paused in '+(d.since||'')+' → '+(d.until||''));
  h+=hdKpi('Daily budget freed',fmtINR(freed),'sum of their last ₹/day');
  h+=hdKpi('Spend before closure',fmtINR(spent),'in this window');
  h+='</div></section>';
  h+='<section class="section"><div class="card"><h3>Closed budget — campaigns switched off</h3><div class="csub">A campaign closed mid-window drops out of Campaigns and lands here. &ldquo;Closed on&rdquo; = Meta last-modified (closest signal to when it was paused). &ldquo;Age&rdquo; = days from campaign creation to closure.</div><table id="hdClosedTable"><thead><tr><th>Campaign</th><th>Product</th><th>Budget/day</th><th>Closed on</th><th>Age at closure</th><th>Spend</th><th>ROAS</th><th>Orders</th></tr></thead><tbody>';
  cl.forEach(c=>{h+='<tr><td><b>'+hdEsc(c.name)+'</b> <span class="muted" style="font-family:monospace;font-size:11px">'+hdEsc(c.id)+'</span></td>'+
    '<td><span class="chip">'+hdEsc(c.product||'—')+'</span></td>'+
    '<td>'+fmtINR(c.budget)+'</td><td>'+hdEsc(c.closed_on||'—')+'</td>'+
    '<td>'+(c.age_days!=null?fmtNum(c.age_days)+'d old':'—')+'</td>'+
    '<td>'+fmtINR(c.spend)+'</td><td>'+rg(c.roas)+'</td><td>'+fmtNum(c.orders)+'</td></tr>';});
  if(!cl.length) h+='<tr><td colspan="8" class="muted">No campaigns were closed in this window.</td></tr>';
  h+='</tbody></table></div></section>';
  return h;
}
const HD_DUTIES=['<span style="font-weight:900">&#128064; Observe data</span> — <b>not</b> make reports','Create listings','Work on new launches','Check del%','Weekly customer-care audits','Make relevant creatives','Always work to TAT'];
function hdDutyHTML(){return HD_DUTIES.map(x=>'<span class="duty">'+x+'<span class="sep">&bull;</span></span>').join('');}
function hdRender(){
  const dt=document.getElementById('dutyTrack'); if(dt&&!dt.dataset.f){dt.innerHTML=hdDutyHTML()+hdDutyHTML();dt.dataset.f='1';}
  document.getElementById('hdTabs').innerHTML=HD_TABS.map(t=>'<button data-t="'+t[0]+'"'+(t[0]===HDTAB?' class="on"':'')+'>'+t[1]+'</button>').join('');
  [...document.querySelectorAll('#hdTabs button')].forEach(b=>b.onclick=()=>{HDTAB=b.dataset.t;hdRender();});
  const cd=hdCatData(); const presets=Object.keys(HD_PRESET_LABELS).filter(p=>cd&&cd[p]);
  document.getElementById('hdPresetSeg').innerHTML=(presets.length?presets:['yesterday']).map(p=>'<button data-p="'+p+'"'+(p===HDPRESET?' class="on"':'')+'>'+HD_PRESET_LABELS[p]+'</button>').join('');
  [...document.querySelectorAll('#hdPresetSeg button')].forEach(b=>b.onclick=()=>{HDPRESET=b.dataset.p;hdRender();});
  const d=hdPick(), body=document.getElementById('hdBody'), fp=document.getElementById('hdFetched');
  if(!d){ document.getElementById('hdScope').textContent=''; fp.textContent='';
    body.innerHTML='<section class="section"><div class="card"><div class="muted">'+hdEsc(HDCAT)+' data is not embedded in this build (no Meta token at build time). It will populate on the next scheduled build.</div></div></section>'; return; }
  document.getElementById('hdScope').textContent='('+(d.since||'')+' → '+(d.until||'')+')';
  fp.innerHTML='&#128336; Fetched from Meta: <b>'+hdEsc(d.fetched_at||'—')+'</b>'+(d.fetched_ts?' ('+hdAgo(d.fetched_ts)+')':'');
  body.innerHTML = HDTAB==='overview'?hdOverview(d) : HDTAB==='campaigns'?hdCampaigns(d) : HDTAB==='budget'?hdBudget(d) : HDTAB==='products'?hdAllProducts(d) : HDTAB==='plan'?hdPlan(d) : HDTAB==='clearance'?hdClearance(d) : HDTAB==='profit'?hdProfit(d) : HDTAB==='closed'?hdClosed(d) : hdCreative(d);
}
setInterval(()=>{ if(STATE.view==='homedecor'){ const d=hdPick(), fp=document.getElementById('hdFetched'); if(d&&d.fetched_ts&&fp) fp.innerHTML='&#128336; Fetched from Meta: <b>'+hdEsc(d.fetched_at||'—')+'</b> ('+hdAgo(d.fetched_ts)+')'; } }, 60000);
// ===================== /HOME DECOR MODULE =====================

function renderAll(){
  if(STATE.view==='homedecor'){ hdRender(); return; }
  setTitles(); renderBanner(); renderChrome();
  renderLive(); renderOverview(); renderCatBudget(); renderAllocation();
  renderContribution(); renderProfitability(); renderBudgetInHand();
  renderStock(); renderLifecycle(); renderDecisionBoard();
}
function applyView(view,cat){
  STATE.view=view; STATE.cat=cat||null;
  document.querySelectorAll('.item').forEach(x=>x.classList.remove('active'));
  let sel;
  if(view==='homedecor'){ HDCAT=cat||'Crystal Home Decor'; HDTAB='overview'; sel=document.querySelector('.item[data-cat="'+cssEsc(HDCAT)+'"]'); }
  else if(view==='category') sel=document.querySelector('.item[data-cat="'+cssEsc(cat)+'"]');
  else sel=document.querySelector('.item[data-view="home"]');
  if(sel)sel.classList.add('active');
  const hd=view==='homedecor';
  document.getElementById('reportWrap').style.display=hd?'none':'';
  document.getElementById('view-homedecor').style.display=hd?'':'none';
  if(hd){
    const nm=document.getElementById('hdName'); if(nm)nm.textContent=HDCAT;
    const ic=document.getElementById('hdIcon'); if(ic)ic.innerHTML=CAT_ICON[HDCAT]||'&#9670;';
    document.getElementById('crumb').innerHTML=esc(HDCAT)+'<small>category module · live from Meta (embedded)</small>';
  }
  window.scrollTo({top:0});
  renderAll();
}
function cssEsc(s){return (s||'').replace(/"/g,'\\"');}

// ---------- category nav ----------
(function(){
  const tot={}; P.catRev.forEach(r=>{tot[r.c]=(tot[r.c]||0)+r.rev;});
  // Meta-only categories (live module, possibly no Shopify revenue) still get a sidebar entry.
  Object.keys(CATDATA||{}).forEach(c=>{ if(!(c in tot)) tot[c]=0; });
  const cats=Object.keys(tot).sort((a,b)=>tot[b]-tot[a]);
  let h=''; cats.forEach(c=>{h+='<div class="item" data-cat="'+esc(c)+'"><span class="ic">'+(CAT_ICON[c]||'&#9670;')+'</span> '+esc(c)+'</div>';});
  document.getElementById('catnav').innerHTML=h;
})();

// ---------- portal segmented control ----------
(function(){const seg=document.getElementById('portalseg');
  ['ALL'].concat(PORTALS).forEach(p=>{const b=document.createElement('button');
    b.textContent=p; if(p===STATE.portal)b.className='on';
    b.onclick=()=>{STATE.portal=p;[...seg.children].forEach(c=>c.classList.remove('on'));b.classList.add('on');STATE.dues.meta=null;renderAll();};
    seg.appendChild(b);});})();

// ---------- calendar ----------
const dFrom=document.getElementById('dFrom'), dTo=document.getElementById('dTo');
[dFrom,dTo].forEach(el=>{el.min=P.minDate;el.max=P.maxDate;});
dFrom.value=STATE.from; dTo.value=STATE.to;
dFrom.onchange=()=>{STATE.from=dFrom.value;renderAll();};
dTo.onchange=()=>{STATE.to=dTo.value;renderAll();};

// ---------- dues inputs ----------
['Meta','Courier','Vendor'].forEach(k=>{const el=document.getElementById('due'+k);
  el.onchange=()=>{STATE.dues[k.toLowerCase()]=+el.value||0;saveCfg();renderBudgetInHand();};});

// ---------- sidebar clicks ----------
document.querySelectorAll('.item').forEach(it=>it.onclick=()=>{
  if(it.dataset.cat){ applyView(hdHasCat(it.dataset.cat)?'homedecor':'category', it.dataset.cat); }
  else if(it.dataset.view==='home'){ applyView('home',null); }
  else if(it.dataset.go){ const t=document.getElementById(it.dataset.go); if(t)t.scrollIntoView({behavior:'smooth',block:'start'}); }
});

// ---------- sortable tables ----------
function wireSort(tableId,sortObj,render){
  document.querySelectorAll('#'+tableId+' thead th').forEach(th=>{if(!th.dataset.k)return;
    th.onclick=()=>{const k=th.dataset.k; if(sortObj.k===k)sortObj.dir*=-1; else{sortObj.k=k;sortObj.dir=-1;} render();};});
}
wireSort('catTable',catSort,renderCatBudget);
wireSort('profitTable',profSort,renderProfitability);

document.getElementById('foot').textContent='span '+P.minDate+' .. '+P.maxDate;
renderAll();
</script>
</body>
</html>
"""


if __name__ == '__main__':
    main()
