#!/usr/bin/env python3
"""
NTN Dashboard v2 — analytical dashboard generator (rewrite).

Reads from state/ntn.db, dumps the full ad-day grid + lookups as JSON, and
renders a single-page HTML where filters/sorting/aggregation all happen
client-side. Once loaded, the dashboard re-renders every filter combination
in <100ms with no API calls and no re-fetch.

Architecture:
  - DB → JSON payload (one row per ad-day, plus lookups)
  - HTML with embedded data + Chart.js
  - JS filters/aggregates on every interaction

Usage:
  python3 scripts/v2/build_dashboard.py
  python3 scripts/v2/build_dashboard.py --days 60
  python3 scripts/v2/build_dashboard.py --out custom.html
"""

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _utils import db_connect, IST, now_iso  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
OUT_DIR = REPO_ROOT / 'out'
OUT_DIR.mkdir(parents=True, exist_ok=True)


def fetch_ad_days(conn, since: str, until: str):
    """One row per (ad_id, date) with everything the dashboard needs.
    JOINs meta_ads_meta for classifications + lifetime stats.
    Filtered to ads with spend>0 in the period to keep payload small."""
    rows = conn.execute('''
        SELECT
          d.date,
          d.ad_id,
          d.portal,
          d.account_name,
          d.campaign_id,
          d.campaign_name,
          d.adset_id,
          d.adset_name,
          d.ad_name,
          d.spend,
          d.impressions,
          d.reach,
          d.clicks,
          d.inline_link_clicks,
          d.outbound_clicks,
          d.ctr,
          d.cpm,
          d.cpc,
          d.frequency,
          d.purchases,
          d.revenue,
          d.add_to_cart,
          d.landing_page_views,
          d.video_thruplay,
          m.category,
          m.creative_type,
          COALESCE(sl.label, m.sentiment) AS sentiment,
          m.sentiment AS sentiment_code,
          COALESCE(m.product, p.product, m.ntn_code, '(no product tag)') AS product,
          m.ntn_code,
          m.first_seen,
          m.last_seen,
          m.days_active,
          m.total_spend AS lifetime_spend,
          m.total_revenue AS lifetime_revenue,
          m.total_purchases AS lifetime_purchases
        FROM meta_ads_daily d
        LEFT JOIN meta_ads_meta  m ON d.ad_id = m.ad_id
        LEFT JOIN sentiment_labels sl ON sl.code = m.sentiment
        LEFT JOIN product_ntn_labels p ON p.ntn_code = m.ntn_code
        WHERE d.date BETWEEN ? AND ? AND d.spend > 0
        ORDER BY d.date, d.ad_id
    ''', (since, until)).fetchall()
    cols = ['date', 'ad_id', 'portal', 'account_name', 'campaign_id',
            'campaign_name', 'adset_id', 'adset_name', 'ad_name',
            'spend', 'impressions', 'reach', 'clicks', 'inline_link_clicks',
            'outbound_clicks', 'ctr', 'cpm', 'cpc', 'frequency',
            'purchases', 'revenue', 'add_to_cart', 'landing_page_views',
            'video_thruplay',
            'category', 'creative_type', 'sentiment', 'sentiment_code',
            'product', 'ntn_code', 'first_seen', 'last_seen', 'days_active',
            'lifetime_spend', 'lifetime_revenue', 'lifetime_purchases']
    out = []
    for r in rows:
        d = dict(zip(cols, r))
        # Round/clean numerics for smaller payload
        for k in ('spend', 'revenue', 'cpm', 'cpc', 'frequency',
                  'lifetime_spend', 'lifetime_revenue'):
            if d.get(k) is not None:
                d[k] = round(float(d[k]), 2) if d[k] else 0
        for k in ('ctr',):
            if d.get(k) is not None:
                d[k] = round(float(d[k]), 3) if d[k] else 0
        # Strip null fields to shrink payload
        d = {k: v for k, v in d.items() if v is not None}
        out.append(d)
    return out


def fetch_dimensions(conn, since: str, until: str):
    """Pre-compute distinct filter values + their counts so dropdowns are
    quick to populate without scanning the full payload."""
    def distinct(col):
        # Note: literal column substitution is safe — values are hard-coded
        return [r[0] for r in conn.execute(
            f'''SELECT DISTINCT {col} FROM meta_ads_daily d
                LEFT JOIN meta_ads_meta m ON d.ad_id = m.ad_id
                WHERE d.date BETWEEN ? AND ? AND {col} IS NOT NULL
                ORDER BY {col}''',
            (since, until)
        ).fetchall()]
    return {
        'portals':        distinct('d.portal'),
        'categories':     distinct('m.category'),
        'creative_types': distinct('m.creative_type'),
        'sentiments':     distinct('m.sentiment'),
        'products':       distinct(
            "COALESCE(m.product, m.ntn_code, '(no product tag)')"
        ),
        'accounts':       distinct('d.account_name'),
    }


def fetch_freshness(conn):
    out = {}
    for r in conn.execute('''
        SELECT job_name, target_date, status, started_at, finished_at, rows_written
        FROM ingest_log
        WHERE (job_name, started_at) IN (
          SELECT job_name, MAX(started_at) FROM ingest_log GROUP BY job_name
        )
    ''').fetchall():
        out[r[0]] = {
            'target_date': r[1], 'status': r[2],
            'started_at': r[3], 'finished_at': r[4],
            'rows_written': r[5],
        }
    return out


# Main HTML — kept in a separate function to avoid massive escape soup
def render_html(payload_json: str, since: str, until: str) -> str:
    return HTML_TEMPLATE.replace('__PAYLOAD__', payload_json).replace(
        '__SINCE__', since).replace('__UNTIL__', until)


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>NTN Analytics — Categories v2</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
* { margin:0; padding:0; box-sizing:border-box; }
body { font-family:-apple-system,'Segoe UI',Roboto,sans-serif; background:#f0f4fb; color:#1a1a2e; font-size:13px; }
a { color:#1a3d7c; text-decoration:none; }
.header { background:linear-gradient(135deg,#0d2145,#1a3d7c); padding:14px 22px; color:#fff; display:flex; align-items:center; justify-content:space-between; flex-wrap:wrap; gap:10px; }
.header h1 { font-size:18px; }
.header-meta { font-size:11px; color:rgba(255,255,255,.7); }
.last-upd { font-size:10px; color:rgba(255,255,255,.5); }

.controls { background:#fff; padding:14px 22px; border-bottom:1px solid #dde3f0; display:flex; gap:14px; flex-wrap:wrap; align-items:center; position:sticky; top:0; z-index:50; box-shadow:0 1px 4px rgba(0,0,0,.04); }
.ctrl-group { display:flex; flex-direction:column; gap:3px; }
.ctrl-lbl { font-size:9px; font-weight:700; text-transform:uppercase; letter-spacing:.5px; color:#6b7280; }
.ctrl-input, .ctrl-select { padding:6px 9px; border:1px solid #dde3f0; border-radius:6px; font-size:12px; min-width:120px; background:#fff; }
.preset-btns { display:flex; gap:5px; }
.preset-btn { padding:6px 10px; border:1px solid #dde3f0; background:#fff; border-radius:6px; font-size:11px; cursor:pointer; font-weight:600; }
.preset-btn.active { background:#1a3d7c; color:#fff; border-color:#1a3d7c; }
.btn-clear { background:#fff5f5; color:#a3260a; border:1px solid #fed7d7; padding:6px 10px; border-radius:6px; font-size:11px; cursor:pointer; font-weight:700; }
.multi-sel { display:flex; gap:4px; flex-wrap:wrap; max-width:280px; }
.chip { padding:3px 8px; border-radius:14px; background:#eef2ff; color:#1a3d7c; font-size:10px; font-weight:700; cursor:pointer; border:1px solid #dde3f0; }
.chip.active { background:#1a3d7c; color:#fff; border-color:#1a3d7c; }

.main { max-width:1700px; margin:0 auto; padding:18px 22px; }
.kpi-strip { display:grid; grid-template-columns:repeat(auto-fit,minmax(170px,1fr)); gap:10px; margin-bottom:18px; }
.kpi-card { background:#fff; padding:12px 14px; border-radius:10px; border:1px solid #dde3f0; }
.kpi-lbl { font-size:9px; text-transform:uppercase; letter-spacing:.5px; color:#6b7280; font-weight:700; }
.kpi-val { font-size:22px; font-weight:800; color:#0d2145; margin-top:2px; }
.kpi-sub { font-size:10px; color:#6b7280; margin-top:2px; display:flex; gap:6px; align-items:center; }
.delta-up { color:#059669; }
.delta-down { color:#dc2626; }

.section { background:#fff; border-radius:10px; padding:14px 16px; border:1px solid #dde3f0; margin-bottom:14px; }
.section h2 { font-size:13px; font-weight:800; color:#0d2145; margin-bottom:10px; padding-bottom:6px; border-bottom:1px solid #eef2ff; display:flex; align-items:center; justify-content:space-between; }
.section h2 .meta { font-weight:400; font-size:10px; color:#6b7280; }
.charts-row { display:grid; grid-template-columns:1fr 1fr; gap:14px; }
@media (max-width:1100px) { .charts-row { grid-template-columns:1fr; } }
.chart-wrap { position:relative; height:240px; }

table { width:100%; border-collapse:collapse; font-size:11px; }
th { background:#f8faff; color:#374151; padding:7px 9px; text-align:left; font-size:10px; text-transform:uppercase; letter-spacing:.4px; border-bottom:1px solid #dde3f0; cursor:pointer; user-select:none; white-space:nowrap; }
th:not(:first-child) { text-align:right; }
th.sorted-asc::after { content:' ↑'; color:#1a3d7c; }
th.sorted-desc::after { content:' ↓'; color:#1a3d7c; }
td { padding:7px 9px; border-bottom:1px solid #f3f4f6; }
td:not(:first-child) { text-align:right; font-variant-numeric:tabular-nums; }
tr:hover td { background:#fafbff; }
.rg { color:#059669; font-weight:700; }
.ro { color:#d97706; font-weight:700; }
.rr { color:#dc2626; font-weight:700; }
.tag { display:inline-block; padding:2px 7px; border-radius:5px; font-size:10px; font-weight:700; }
.tag-sm  { background:#dbeafe; color:#1d4ed8; }
.tag-sml { background:#d1fae5; color:#065f46; }
.tag-nbp { background:#fef3c7; color:#92400e; }
.subtle { color:#9ca3af; font-size:10px; }
.empty { padding:30px 14px; text-align:center; color:#9ca3af; font-style:italic; }
.cell-name { max-width:340px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.btn-csv { background:#1a3d7c; color:#fff; border:none; padding:6px 11px; border-radius:6px; font-size:10px; cursor:pointer; font-weight:700; }
.btn-csv:hover { background:#0d2145; }

/* Heatmap-ish coloring for success rate cells */
.sr-0 { background:#fef2f2; color:#7f1d1d; }
.sr-low { background:#fef3c7; color:#78350f; }
.sr-med { background:#dcfce7; color:#14532d; }
.sr-hi { background:#bbf7d0; color:#065f46; font-weight:700; }
</style>
</head>
<body>

<div class="header">
  <div>
    <h1>📊 NTN Categories — Analytics</h1>
    <div class="header-meta">DB-backed · Period: __SINCE__ → __UNTIL__ · all filters operate on the embedded dataset</div>
    <div class="last-upd" id="last-upd"></div>
  </div>
  <nav style="font-size:11px">
    <a href="/" style="color:rgba(255,255,255,.85);border-bottom:1px dashed rgba(255,255,255,.5)">📊 NTN</a>&nbsp;&nbsp;
    <a href="/today_live.html" style="color:rgba(255,255,255,.85);border-bottom:1px dashed rgba(255,255,255,.5)">🔴 Today</a>&nbsp;&nbsp;
    <a href="/categories" style="color:rgba(255,255,255,.85);border-bottom:1px dashed rgba(255,255,255,.5)">📂 Old Categories</a>&nbsp;&nbsp;
    <span style="color:#fff;border-bottom:2px solid #fff">📂 v2</span>
  </nav>
</div>

<div class="controls">
  <div class="ctrl-group">
    <span class="ctrl-lbl">Date Range</span>
    <div class="preset-btns" id="preset-btns">
      <button class="preset-btn" data-days="1">Today</button>
      <button class="preset-btn" data-days="3">3D</button>
      <button class="preset-btn active" data-days="7">7D</button>
      <button class="preset-btn" data-days="14">14D</button>
      <button class="preset-btn" data-days="30">30D</button>
      <button class="preset-btn" data-days="all">All</button>
    </div>
  </div>
  <div class="ctrl-group">
    <span class="ctrl-lbl">Custom From</span>
    <input type="date" class="ctrl-input" id="from-date">
  </div>
  <div class="ctrl-group">
    <span class="ctrl-lbl">Custom To</span>
    <input type="date" class="ctrl-input" id="to-date">
  </div>
  <div class="ctrl-group">
    <span class="ctrl-lbl">Portal</span>
    <div class="multi-sel" id="filter-portals"></div>
  </div>
  <div class="ctrl-group">
    <span class="ctrl-lbl">Category</span>
    <div class="multi-sel" id="filter-categories"></div>
  </div>
  <div class="ctrl-group">
    <span class="ctrl-lbl">Creative Type</span>
    <div class="multi-sel" id="filter-creatives"></div>
  </div>
  <div class="ctrl-group">
    <span class="ctrl-lbl">Product</span>
    <select class="ctrl-select" id="filter-product">
      <option value="">All Products</option>
    </select>
  </div>
  <div class="ctrl-group">
    <span class="ctrl-lbl">&nbsp;</span>
    <button class="btn-clear" id="btn-clear">Clear All Filters</button>
  </div>
</div>

<div class="main">
  <div class="kpi-strip" id="kpi-strip"></div>

  <div class="section">
    <h2>📈 Trends Over Time <span class="meta" id="trend-meta"></span></h2>
    <div class="charts-row">
      <div class="chart-wrap"><canvas id="chart-spend-rev"></canvas></div>
      <div class="chart-wrap"><canvas id="chart-roas"></canvas></div>
    </div>
    <div class="charts-row" style="margin-top:14px">
      <div class="chart-wrap"><canvas id="chart-orders"></canvas></div>
      <div class="chart-wrap"><canvas id="chart-cat-spend"></canvas></div>
    </div>
  </div>

  <div class="section">
    <h2>📂 Per-Category Breakdown <span class="meta">click rows to drill</span></h2>
    <table id="tbl-categories">
      <thead><tr>
        <th data-col="category" data-type="str">Category</th>
        <th data-col="active_ads" data-type="num">Active Ads</th>
        <th data-col="active_camps" data-type="num">Camps</th>
        <th data-col="spend" data-type="num">Spend (₹)</th>
        <th data-col="revenue" data-type="num">Revenue (₹)</th>
        <th data-col="purchases" data-type="num">Orders</th>
        <th data-col="roas" data-type="num">ROAS</th>
        <th data-col="cpm" data-type="num">CPM (₹)</th>
        <th data-col="ctr" data-type="num">CTR (%)</th>
        <th data-col="atc_rate" data-type="num">ATC%</th>
        <th data-col="success_rate" data-type="num">Success% (7d)</th>
      </tr></thead>
      <tbody></tbody>
    </table>
  </div>

  <div class="section">
    <h2>🎨 Creative Type × Category Heatmap <span class="meta">spend-weighted ROAS — green ≥2.5, amber 1.5-2.5, red &lt;1.5</span></h2>
    <div style="overflow-x:auto"><table id="tbl-heatmap"></table></div>
  </div>

  <div class="section">
    <h2>🛍️ Product Performance <button class="btn-csv" onclick="exportCSV('products')">↓ CSV</button></h2>
    <table id="tbl-products">
      <thead><tr>
        <th data-col="product" data-type="str">Product</th>
        <th data-col="category" data-type="str">Cat</th>
        <th data-col="active_ads" data-type="num">Ads</th>
        <th data-col="spend" data-type="num">Spend</th>
        <th data-col="revenue" data-type="num">Revenue</th>
        <th data-col="orders" data-type="num">Orders</th>
        <th data-col="roas" data-type="num">ROAS</th>
        <th data-col="hit_15" data-type="num">≥1.5x</th>
        <th data-col="hit_20" data-type="num">≥2.0x</th>
        <th data-col="hit_25" data-type="num">≥2.5x</th>
        <th data-col="hit_30" data-type="num">≥3.0x</th>
        <th data-col="hit_40" data-type="num">≥4.0x</th>
        <th data-col="hit_50" data-type="num">≥5.0x</th>
      </tr></thead>
      <tbody></tbody>
    </table>
  </div>

  <div class="section">
    <h2>🏆 Top + Bottom Ads <button class="btn-csv" onclick="exportCSV('ads')">↓ CSV</button></h2>
    <table id="tbl-ads">
      <thead><tr>
        <th data-col="portal" data-type="str">Portal</th>
        <th data-col="category" data-type="str">Cat</th>
        <th data-col="creative_type" data-type="str">Type</th>
        <th data-col="ad_name" data-type="str">Ad / Campaign</th>
        <th data-col="days_active" data-type="num">Days</th>
        <th data-col="spend" data-type="num">Spend</th>
        <th data-col="revenue" data-type="num">Revenue</th>
        <th data-col="orders" data-type="num">Orders</th>
        <th data-col="roas" data-type="num">ROAS</th>
      </tr></thead>
      <tbody></tbody>
    </table>
  </div>

  <div class="section">
    <h2>💬 Sentiment × Creative Type <span class="meta">Edit sentiment_labels table to set readable names</span></h2>
    <table id="tbl-sentiment">
      <thead><tr>
        <th data-col="sentiment" data-type="str">Sentiment</th>
        <th data-col="creative_type" data-type="str">Creative Type</th>
        <th data-col="active_ads" data-type="num">Ads</th>
        <th data-col="spend" data-type="num">Spend</th>
        <th data-col="revenue" data-type="num">Revenue</th>
        <th data-col="roas" data-type="num">ROAS</th>
      </tr></thead>
      <tbody></tbody>
    </table>
  </div>
</div>

<script>
const PAYLOAD = __PAYLOAD__;
const RAW = PAYLOAD.rows;
const DIM = PAYLOAD.dimensions;
const FRESH = PAYLOAD.freshness;
document.getElementById('last-upd').textContent = `Built ${PAYLOAD.updated_at} · ${RAW.length.toLocaleString()} ad-day rows · ${Object.keys(FRESH).length} ingest jobs tracked`;

// ── Filter state ─────────────────────────────────────────────────────────
const F = {
  fromDate: PAYLOAD.since,
  toDate:   PAYLOAD.until,
  portals:  new Set(),       // empty = all
  categories: new Set(),
  creative_types: new Set(),
  product:  '',              // single-select
};

// ── Helpers ─────────────────────────────────────────────────────────────
const fmt = {
  inr: n => n == null ? '—' : '₹' + Math.round(n).toLocaleString('en-IN'),
  num: n => n == null ? '—' : Math.round(n).toLocaleString('en-IN'),
  num1: n => n == null ? '—' : Number(n).toFixed(1),
  pct: n => n == null ? '—' : Number(n).toFixed(1) + '%',
  roas: n => {
    if (n == null) return '—';
    const cls = n >= 2.5 ? 'rg' : n >= 1.5 ? 'ro' : 'rr';
    return `<span class="${cls}">${Number(n).toFixed(2)}x</span>`;
  },
  delta: (cur, prev) => {
    if (prev == null || prev === 0) return '';
    const pct = ((cur - prev) / prev) * 100;
    const cls = pct >= 0 ? 'delta-up' : 'delta-down';
    const arrow = pct >= 0 ? '▲' : '▼';
    return `<span class="${cls}">${arrow} ${Math.abs(pct).toFixed(1)}%</span>`;
  },
};
function tag(p) { return `<span class="tag tag-${(p||'').toLowerCase()}">${p||'?'}</span>`; }

// ── Initial filter UI population ────────────────────────────────────────
function buildChips(containerId, values, set) {
  const c = document.getElementById(containerId);
  c.innerHTML = '';
  values.forEach(v => {
    const chip = document.createElement('span');
    chip.className = 'chip';
    chip.textContent = v;
    chip.dataset.val = v;
    chip.addEventListener('click', () => {
      if (set.has(v)) { set.delete(v); chip.classList.remove('active'); }
      else { set.add(v); chip.classList.add('active'); }
      apply();
    });
    c.appendChild(chip);
  });
}
buildChips('filter-portals',    DIM.portals,        F.portals);
buildChips('filter-categories', DIM.categories,     F.categories);
buildChips('filter-creatives',  DIM.creative_types, F.creative_types);

// Product dropdown
const prodSel = document.getElementById('filter-product');
DIM.products.forEach(p => {
  const o = document.createElement('option');
  o.value = p; o.textContent = p;
  prodSel.appendChild(o);
});
prodSel.addEventListener('change', e => { F.product = e.target.value; apply(); });

// Date inputs
document.getElementById('from-date').value = F.fromDate;
document.getElementById('to-date').value   = F.toDate;
document.getElementById('from-date').addEventListener('change', e => { F.fromDate = e.target.value; clearActivePreset(); apply(); });
document.getElementById('to-date').addEventListener('change',   e => { F.toDate   = e.target.value; clearActivePreset(); apply(); });

// Date presets
document.querySelectorAll('.preset-btn').forEach(b => {
  b.addEventListener('click', () => {
    document.querySelectorAll('.preset-btn').forEach(x => x.classList.remove('active'));
    b.classList.add('active');
    const days = b.dataset.days;
    if (days === 'all') {
      F.fromDate = PAYLOAD.since;
      F.toDate   = PAYLOAD.until;
    } else {
      const end = new Date(PAYLOAD.until + 'T00:00:00');
      const n = parseInt(days, 10);
      const start = new Date(end); start.setDate(end.getDate() - (n - 1));
      F.fromDate = start.toISOString().slice(0, 10);
      F.toDate   = PAYLOAD.until;
    }
    document.getElementById('from-date').value = F.fromDate;
    document.getElementById('to-date').value   = F.toDate;
    apply();
  });
});
function clearActivePreset() {
  document.querySelectorAll('.preset-btn').forEach(x => x.classList.remove('active'));
}

document.getElementById('btn-clear').addEventListener('click', () => {
  F.portals.clear(); F.categories.clear(); F.creative_types.clear(); F.product = '';
  document.querySelectorAll('.chip').forEach(c => c.classList.remove('active'));
  prodSel.value = '';
  apply();
});

// ── Filter logic ────────────────────────────────────────────────────────
function applyFilters(rows) {
  return rows.filter(r => {
    if (r.date < F.fromDate || r.date > F.toDate) return false;
    if (F.portals.size && !F.portals.has(r.portal)) return false;
    if (F.categories.size && !F.categories.has(r.category)) return false;
    if (F.creative_types.size && !F.creative_types.has(r.creative_type)) return false;
    if (F.product && r.product !== F.product) return false;
    return true;
  });
}

function getCompareSet() {
  // For trend deltas: compare to previous equal-length window
  const start = new Date(F.fromDate + 'T00:00:00');
  const end   = new Date(F.toDate   + 'T00:00:00');
  const ms    = end - start;
  const prevEnd   = new Date(start); prevEnd.setDate(start.getDate() - 1);
  const prevStart = new Date(prevEnd); prevStart.setTime(prevEnd.getTime() - ms);
  const prevFrom = prevStart.toISOString().slice(0, 10);
  const prevTo   = prevEnd.toISOString().slice(0, 10);
  return RAW.filter(r => {
    if (r.date < prevFrom || r.date > prevTo) return false;
    if (F.portals.size && !F.portals.has(r.portal)) return false;
    if (F.categories.size && !F.categories.has(r.category)) return false;
    if (F.creative_types.size && !F.creative_types.has(r.creative_type)) return false;
    if (F.product && r.product !== F.product) return false;
    return true;
  });
}

// ── Aggregations ────────────────────────────────────────────────────────
function aggregate(rows, groupKey) {
  const map = new Map();
  for (const r of rows) {
    const k = groupKey ? r[groupKey] : '__total__';
    if (!map.has(k)) map.set(k, {
      key:k, active_ads:new Set(), active_camps:new Set(),
      spend:0, revenue:0, purchases:0, impressions:0, clicks:0,
      atc:0, lpv:0,
    });
    const a = map.get(k);
    a.active_ads.add(r.ad_id);
    a.active_camps.add(r.campaign_id);
    a.spend       += r.spend       || 0;
    a.revenue     += r.revenue     || 0;
    a.purchases   += r.purchases   || 0;
    a.impressions += r.impressions || 0;
    a.clicks      += r.clicks      || 0;
    a.atc         += r.add_to_cart || 0;
    a.lpv         += r.landing_page_views || 0;
  }
  return [...map.values()].map(a => ({
    key:a.key,
    active_ads: a.active_ads.size,
    active_camps: a.active_camps.size,
    spend: a.spend, revenue: a.revenue, purchases: a.purchases,
    impressions: a.impressions, clicks: a.clicks,
    atc: a.atc, lpv: a.lpv,
    roas: a.spend > 0 ? a.revenue / a.spend : 0,
    cpm: a.impressions > 0 ? (a.spend / a.impressions) * 1000 : 0,
    ctr: a.impressions > 0 ? (a.clicks / a.impressions) * 100 : 0,
    atc_rate: a.clicks > 0 ? (a.atc / a.clicks) * 100 : 0,
  }));
}

// ── Success rate: for ads first_seen in window, fraction that ran 7+ days
function successRate(rows, threshold = 7) {
  const seen = new Set();
  const buckets = { launched: 0, survived: 0 };
  for (const r of rows) {
    if (seen.has(r.ad_id)) continue;
    seen.add(r.ad_id);
    if (!r.first_seen || r.first_seen < F.fromDate || r.first_seen > F.toDate) continue;
    buckets.launched++;
    if ((r.days_active || 0) >= threshold) buckets.survived++;
  }
  return buckets.launched > 0 ? (100 * buckets.survived / buckets.launched) : null;
}

// ── KPI strip render ─────────────────────────────────────────────────────
function renderKPI(rows, prevRows) {
  const a = aggregate(rows)[0] || { spend:0, revenue:0, purchases:0, active_ads:0, active_camps:0, roas:0, cpm:0, ctr:0 };
  const p = aggregate(prevRows)[0] || { spend:0, revenue:0, purchases:0, roas:0 };
  const cards = [
    { l:'Active Ads',  v: fmt.num(a.active_ads), s: a.active_camps + ' campaigns' },
    { l:'Spend',       v: fmt.inr(a.spend), s: fmt.delta(a.spend, p.spend) + ' vs prev' },
    { l:'Revenue',     v: fmt.inr(a.revenue), s: fmt.delta(a.revenue, p.revenue) + ' vs prev' },
    { l:'Orders',      v: fmt.num(a.purchases), s: fmt.delta(a.purchases, p.purchases) + ' vs prev' },
    { l:'ROAS',        v: fmt.roas(a.roas), s: 'spend-weighted' },
    { l:'CPM',         v: fmt.inr(a.cpm), s: 'cost / 1k impr' },
    { l:'CTR',         v: fmt.pct(a.ctr), s: 'click-through' },
    { l:'Success Rate (7d)', v: (() => { const s = successRate(rows); return s == null ? '—' : fmt.pct(s); })(), s: 'ads launched in period · survived 7d' },
  ];
  document.getElementById('kpi-strip').innerHTML = cards.map(c =>
    `<div class="kpi-card"><div class="kpi-lbl">${c.l}</div>` +
    `<div class="kpi-val">${c.v}</div><div class="kpi-sub">${c.s}</div></div>`
  ).join('');
}

// ── Time series ─────────────────────────────────────────────────────────
let charts = {};
function destroyCharts() {
  Object.values(charts).forEach(c => c.destroy());
  charts = {};
}

function buildTimeSeries(rows) {
  const by = new Map();
  for (const r of rows) {
    if (!by.has(r.date)) by.set(r.date, { spend:0, revenue:0, orders:0 });
    const b = by.get(r.date);
    b.spend     += r.spend     || 0;
    b.revenue   += r.revenue   || 0;
    b.orders    += r.purchases || 0;
  }
  const sorted = [...by.entries()].sort((a, b) => a[0].localeCompare(b[0]));
  return {
    labels: sorted.map(([d]) => d.slice(5)),  // MM-DD
    spend: sorted.map(([_, v]) => Math.round(v.spend)),
    revenue: sorted.map(([_, v]) => Math.round(v.revenue)),
    orders: sorted.map(([_, v]) => v.orders),
    roas: sorted.map(([_, v]) => v.spend > 0 ? +(v.revenue / v.spend).toFixed(2) : 0),
  };
}

function renderCharts(rows) {
  destroyCharts();
  const ts = buildTimeSeries(rows);
  document.getElementById('trend-meta').textContent =
    `${ts.labels.length} days · spend-weighted`;

  charts.spendRev = new Chart(document.getElementById('chart-spend-rev'), {
    type:'line',
    data:{ labels: ts.labels, datasets: [
      { label:'Spend',   data: ts.spend,   borderColor:'#1a3d7c', backgroundColor:'#1a3d7c33', fill:true, tension:.25 },
      { label:'Revenue', data: ts.revenue, borderColor:'#059669', backgroundColor:'#05966933', fill:true, tension:.25 },
    ]},
    options:{ responsive:true, maintainAspectRatio:false,
      plugins:{ title:{ display:true, text:'Spend vs Revenue (₹)'} },
      scales:{ y:{ ticks:{ callback:v => '₹' + (v >= 100000 ? (v/100000).toFixed(1)+'L' : v.toLocaleString()) } } } },
  });
  charts.roas = new Chart(document.getElementById('chart-roas'), {
    type:'line',
    data:{ labels: ts.labels, datasets: [
      { label:'Daily ROAS', data: ts.roas, borderColor:'#d97706', backgroundColor:'#d9770633', fill:true, tension:.25 },
    ]},
    options:{ responsive:true, maintainAspectRatio:false,
      plugins:{ title:{ display:true, text:'Blended ROAS by Day'} } },
  });
  charts.orders = new Chart(document.getElementById('chart-orders'), {
    type:'bar',
    data:{ labels: ts.labels, datasets: [
      { label:'Orders', data: ts.orders, backgroundColor:'#1a3d7c' },
    ]},
    options:{ responsive:true, maintainAspectRatio:false,
      plugins:{ title:{ display:true, text:'Orders by Day'} } },
  });

  // Category bar chart
  const cats = aggregate(rows, 'category').filter(x => x.key).sort((a, b) => b.spend - a.spend);
  charts.catSpend = new Chart(document.getElementById('chart-cat-spend'), {
    type:'bar',
    data:{ labels: cats.map(c => c.key),
      datasets:[
        { label:'Spend (₹)', data: cats.map(c => Math.round(c.spend)), backgroundColor:'#1a3d7c', yAxisID:'y' },
        { label:'ROAS',      data: cats.map(c => +c.roas.toFixed(2)),  backgroundColor:'#059669', type:'line', yAxisID:'y1', tension:.25 },
      ]},
    options:{ responsive:true, maintainAspectRatio:false,
      plugins:{ title:{ display:true, text:'Spend & ROAS by Category'} },
      scales:{
        y:{ beginAtZero:true, position:'left', ticks:{ callback:v => '₹' + (v >= 100000 ? (v/100000).toFixed(1)+'L' : v) } },
        y1:{ beginAtZero:true, position:'right', grid:{ drawOnChartArea:false } },
      } },
  });
}

// ── Tables ──────────────────────────────────────────────────────────────
const sortState = {};   // tableId → {col, dir}
function setupSort(tableId, defaultCol, defaultDir = 'desc') {
  sortState[tableId] = { col: defaultCol, dir: defaultDir };
  const ths = document.querySelectorAll(`#${tableId} thead th`);
  ths.forEach(th => {
    th.addEventListener('click', () => {
      const col = th.dataset.col;
      const cur = sortState[tableId];
      if (cur.col === col) cur.dir = cur.dir === 'asc' ? 'desc' : 'asc';
      else { cur.col = col; cur.dir = 'desc'; }
      apply();
    });
  });
}
function applySortHeaders(tableId) {
  const { col, dir } = sortState[tableId];
  document.querySelectorAll(`#${tableId} thead th`).forEach(th => {
    th.classList.remove('sorted-asc', 'sorted-desc');
    if (th.dataset.col === col) th.classList.add('sorted-' + dir);
  });
}
function sortRows(rows, tableId) {
  const { col, dir } = sortState[tableId];
  const factor = dir === 'asc' ? 1 : -1;
  return rows.slice().sort((a, b) => {
    const va = a[col], vb = b[col];
    if (va == null) return 1; if (vb == null) return -1;
    if (typeof va === 'number') return (va - vb) * factor;
    return String(va).localeCompare(String(vb)) * factor;
  });
}

function renderCategoriesTable(rows) {
  const cats = aggregate(rows, 'category').filter(x => x.key);
  // Add success rate per category
  for (const c of cats) {
    const subset = rows.filter(r => r.category === c.key);
    c.success_rate = successRate(subset);
    c.category = c.key;
  }
  const sorted = sortRows(cats, 'tbl-categories');
  applySortHeaders('tbl-categories');
  document.querySelector('#tbl-categories tbody').innerHTML = sorted.map(c =>
    `<tr>
      <td><strong>${c.category}</strong></td>
      <td>${fmt.num(c.active_ads)}</td>
      <td>${fmt.num(c.active_camps)}</td>
      <td>${fmt.inr(c.spend)}</td>
      <td>${fmt.inr(c.revenue)}</td>
      <td>${fmt.num(c.purchases)}</td>
      <td>${fmt.roas(c.roas)}</td>
      <td>${fmt.inr(c.cpm)}</td>
      <td>${fmt.num1(c.ctr)}</td>
      <td>${fmt.num1(c.atc_rate)}</td>
      <td>${c.success_rate == null ? '<span class="subtle">—</span>' : fmt.pct(c.success_rate)}</td>
    </tr>`
  ).join('') || '<tr><td colspan="11" class="empty">No data for current filter.</td></tr>';
}

function renderHeatmap(rows) {
  const cats = [...new Set(rows.map(r => r.category).filter(Boolean))].sort();
  const cts  = [...new Set(rows.map(r => r.creative_type).filter(Boolean))].sort();
  let html = '<thead><tr><th>Category ↓ / Creative Type →</th>';
  cts.forEach(ct => html += `<th>${ct}</th>`);
  html += '<th>TOTAL</th></tr></thead><tbody>';
  cats.forEach(cat => {
    html += `<tr><td><strong>${cat}</strong></td>`;
    let totSpend = 0, totRev = 0;
    cts.forEach(ct => {
      const subset = rows.filter(r => r.category === cat && r.creative_type === ct);
      const a = aggregate(subset)[0];
      if (!a) { html += '<td class="subtle">—</td>'; return; }
      totSpend += a.spend; totRev += a.revenue;
      const cls = a.roas >= 2.5 ? 'rg' : a.roas >= 1.5 ? 'ro' : 'rr';
      html += `<td><span class="${cls}">${a.roas.toFixed(2)}x</span><br><span class="subtle">${fmt.inr(a.spend)}</span></td>`;
    });
    const rowRoas = totSpend > 0 ? (totRev / totSpend) : 0;
    const cls = rowRoas >= 2.5 ? 'rg' : rowRoas >= 1.5 ? 'ro' : 'rr';
    html += `<td><span class="${cls}"><strong>${rowRoas.toFixed(2)}x</strong></span><br><span class="subtle">${fmt.inr(totSpend)}</span></td>`;
    html += '</tr>';
  });
  html += '</tbody>';
  document.getElementById('tbl-heatmap').innerHTML = html;
}

function renderProductsTable(rows) {
  // Aggregate by product (within current filter)
  const prods = aggregate(rows, 'product').filter(x => x.key);
  // For each product, compute success-at-ROAS thresholds for ads launched in window
  const adIdsByProd = new Map();
  const adFirstSeen = new Map();
  const adCategory  = new Map();
  for (const r of rows) {
    if (!adIdsByProd.has(r.product)) adIdsByProd.set(r.product, new Map());
    const m = adIdsByProd.get(r.product);
    if (!m.has(r.ad_id)) m.set(r.ad_id, { spend:0, revenue:0, purchases:0 });
    const a = m.get(r.ad_id);
    a.spend     += r.spend     || 0;
    a.revenue   += r.revenue   || 0;
    a.purchases += r.purchases || 0;
    adFirstSeen.set(r.ad_id, r.first_seen);
    adCategory.set(r.ad_id, r.category);
  }
  const ROASBKT = [1.5, 2.0, 2.5, 3.0, 4.0, 5.0];
  for (const p of prods) {
    const ads = adIdsByProd.get(p.key) || new Map();
    const launched = [];
    for (const [adId, m] of ads) {
      const fs = adFirstSeen.get(adId);
      if (fs && fs >= F.fromDate && fs <= F.toDate) {
        const adRoas = m.spend > 0 ? m.revenue / m.spend : 0;
        launched.push(adRoas);
      }
    }
    p.product = p.key;
    // Pick first category seen for this product as representative
    const adIds = [...(adIdsByProd.get(p.key) || new Map()).keys()];
    p.category = adIds.length ? (adCategory.get(adIds[0]) || '') : '';
    p.orders = p.purchases;
    ROASBKT.forEach(thr => {
      const key = 'hit_' + String(thr).replace('.', '').padEnd(2, '0').slice(0, 2);
      const hit = launched.filter(r => r >= thr).length;
      p[key] = launched.length > 0 ? Math.round(100 * hit / launched.length * 10) / 10 : null;
      p[key + '_n'] = launched.length;
      p[key + '_h'] = hit;
    });
  }
  const sorted = sortRows(prods, 'tbl-products');
  applySortHeaders('tbl-products');
  function rateCell(r, thr) {
    const key = 'hit_' + String(thr).replace('.', '').padEnd(2, '0').slice(0, 2);
    const v = r[key];
    if (v == null) return '<span class="subtle">—</span>';
    const cls = v >= 50 ? 'sr-hi' : v >= 25 ? 'sr-med' : v >= 10 ? 'sr-low' : 'sr-0';
    return `<span class="${cls}" style="padding:2px 6px;border-radius:4px">${v.toFixed(0)}%</span><br><span class="subtle">${r[key + '_h']}/${r[key + '_n']}</span>`;
  }
  document.querySelector('#tbl-products tbody').innerHTML = sorted.map(p =>
    `<tr>
      <td><strong class="cell-name" title="${p.product}">${p.product}</strong></td>
      <td>${p.category || '<span class="subtle">—</span>'}</td>
      <td>${fmt.num(p.active_ads)}</td>
      <td>${fmt.inr(p.spend)}</td>
      <td>${fmt.inr(p.revenue)}</td>
      <td>${fmt.num(p.orders)}</td>
      <td>${fmt.roas(p.roas)}</td>
      <td>${rateCell(p, 1.5)}</td>
      <td>${rateCell(p, 2.0)}</td>
      <td>${rateCell(p, 2.5)}</td>
      <td>${rateCell(p, 3.0)}</td>
      <td>${rateCell(p, 4.0)}</td>
      <td>${rateCell(p, 5.0)}</td>
    </tr>`
  ).join('') || '<tr><td colspan="13" class="empty">No data.</td></tr>';
}

function renderAdsTable(rows) {
  // Aggregate per ad (within current filter)
  const map = new Map();
  for (const r of rows) {
    if (!map.has(r.ad_id)) map.set(r.ad_id, {
      ad_id:r.ad_id, ad_name:r.ad_name, campaign_id:r.campaign_id,
      campaign_name:r.campaign_name, portal:r.portal, category:r.category,
      creative_type:r.creative_type, days_active:r.days_active,
      spend:0, revenue:0, orders:0,
    });
    const a = map.get(r.ad_id);
    a.spend   += r.spend   || 0;
    a.revenue += r.revenue || 0;
    a.orders  += r.purchases || 0;
  }
  const ads = [...map.values()].filter(a => a.spend >= 2000);
  for (const a of ads) a.roas = a.spend > 0 ? a.revenue / a.spend : 0;
  const sorted = sortRows(ads, 'tbl-ads').slice(0, 30);  // top 30 by current sort
  applySortHeaders('tbl-ads');
  document.querySelector('#tbl-ads tbody').innerHTML = sorted.map(a =>
    `<tr>
      <td>${tag(a.portal)}</td>
      <td>${a.category || '<span class="subtle">—</span>'}</td>
      <td>${a.creative_type || '<span class="subtle">—</span>'}</td>
      <td><div class="cell-name" title="${(a.ad_name||'').replace(/"/g,'&quot;')}"><strong>${a.ad_name||'?'}</strong><br><span class="subtle">${a.campaign_name||''}</span></div></td>
      <td>${a.days_active != null ? a.days_active + 'd' : '—'}</td>
      <td>${fmt.inr(a.spend)}</td>
      <td>${fmt.inr(a.revenue)}</td>
      <td>${fmt.num(a.orders)}</td>
      <td>${fmt.roas(a.roas)}</td>
    </tr>`
  ).join('') || '<tr><td colspan="9" class="empty">No ads with >= ₹2K spend in selection.</td></tr>';
}

function renderSentimentTable(rows) {
  // Group by (sentiment, creative_type)
  const map = new Map();
  for (const r of rows) {
    const key = (r.sentiment || '(unset)') + '||' + (r.creative_type || '?');
    if (!map.has(key)) map.set(key, {
      sentiment: r.sentiment || '(unset)',
      sentiment_code: r.sentiment_code,
      creative_type: r.creative_type || '?',
      ads: new Set(), spend:0, revenue:0,
    });
    const a = map.get(key);
    a.ads.add(r.ad_id);
    a.spend   += r.spend   || 0;
    a.revenue += r.revenue || 0;
  }
  const arr = [...map.values()].map(a => ({
    sentiment: a.sentiment,
    creative_type: a.creative_type,
    active_ads: a.ads.size,
    spend: a.spend, revenue: a.revenue,
    roas: a.spend > 0 ? a.revenue / a.spend : 0,
  }));
  const sorted = sortRows(arr, 'tbl-sentiment');
  applySortHeaders('tbl-sentiment');
  document.querySelector('#tbl-sentiment tbody').innerHTML = sorted.map(s =>
    `<tr>
      <td><strong>${s.sentiment}</strong></td>
      <td>${s.creative_type}</td>
      <td>${fmt.num(s.active_ads)}</td>
      <td>${fmt.inr(s.spend)}</td>
      <td>${fmt.inr(s.revenue)}</td>
      <td>${fmt.roas(s.roas)}</td>
    </tr>`
  ).join('') || '<tr><td colspan="6" class="empty">No data.</td></tr>';
}

// ── CSV export ──────────────────────────────────────────────────────────
function exportCSV(which) {
  const rows = applyFilters(RAW);
  let csv = '';
  if (which === 'products') {
    const prods = aggregate(rows, 'product').filter(x => x.key);
    csv = 'Product,Active Ads,Spend,Revenue,Orders,ROAS\n' +
      prods.map(p => `"${p.key}",${p.active_ads},${Math.round(p.spend)},${Math.round(p.revenue)},${p.purchases},${p.roas.toFixed(2)}`).join('\n');
  } else if (which === 'ads') {
    csv = 'Portal,Category,Creative,Ad,Campaign,Spend,Revenue,Orders,ROAS,DaysActive\n';
    const map = new Map();
    for (const r of rows) {
      if (!map.has(r.ad_id)) map.set(r.ad_id, { ...r, _spend:0, _revenue:0, _orders:0 });
      const a = map.get(r.ad_id);
      a._spend += r.spend; a._revenue += r.revenue; a._orders += r.purchases || 0;
    }
    const ads = [...map.values()].filter(a => a._spend >= 1000)
      .sort((a, b) => (b._revenue / Math.max(b._spend, 1)) - (a._revenue / Math.max(a._spend, 1)));
    csv += ads.map(a => {
      const roas = a._spend > 0 ? (a._revenue / a._spend).toFixed(2) : '0';
      return `"${a.portal}","${a.category||''}","${a.creative_type||''}","${(a.ad_name||'').replace(/"/g, '""')}","${(a.campaign_name||'').replace(/"/g, '""')}",${Math.round(a._spend)},${Math.round(a._revenue)},${a._orders},${roas},${a.days_active||''}`;
    }).join('\n');
  }
  const blob = new Blob([csv], { type:'text/csv' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url; a.download = which + '_' + F.fromDate + '_' + F.toDate + '.csv';
  document.body.appendChild(a); a.click(); a.remove();
  URL.revokeObjectURL(url);
}

// ── Main render ─────────────────────────────────────────────────────────
function apply() {
  const rows = applyFilters(RAW);
  const prevRows = getCompareSet();
  renderKPI(rows, prevRows);
  renderCharts(rows);
  renderCategoriesTable(rows);
  renderHeatmap(rows);
  renderProductsTable(rows);
  renderAdsTable(rows);
  renderSentimentTable(rows);
}

// Initialize sort defaults
setupSort('tbl-categories', 'spend');
setupSort('tbl-products',   'spend');
setupSort('tbl-ads',        'roas');
setupSort('tbl-sentiment',  'spend');

// Trigger 7D preset on load
document.querySelector('.preset-btn[data-days="7"]').click();
</script>
</body>
</html>
"""


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--days', type=int, default=30,
                   help='Pull last N days of data (default 30). Larger = more data, larger HTML.')
    p.add_argument('--since', help='YYYY-MM-DD (overrides --days)')
    p.add_argument('--until', help='YYYY-MM-DD (overrides --days)')
    p.add_argument('--out', default=str(OUT_DIR / 'v2' / 'categories.html'))
    p.add_argument('--db', help='SQLite path (default state/ntn.db)')
    args = p.parse_args()

    today = datetime.now(IST).date()
    end = datetime.strptime(args.until, '%Y-%m-%d').date() if args.until else today
    if args.since:
        start = datetime.strptime(args.since, '%Y-%m-%d').date()
    else:
        start = end - timedelta(days=args.days - 1)
    since = start.isoformat()
    until = end.isoformat()

    conn = db_connect(Path(args.db)) if args.db else db_connect()

    print(f"📊 Building analytical dashboard")
    print(f"   Period: {since} → {until}")

    rows = fetch_ad_days(conn, since, until)
    dimensions = fetch_dimensions(conn, since, until)
    freshness = fetch_freshness(conn)

    payload = {
        'since': since,
        'until': until,
        'rows': rows,
        'dimensions': dimensions,
        'freshness': freshness,
        'updated_at': now_iso(),
    }
    payload_json = json.dumps(payload, default=str, ensure_ascii=False, separators=(',', ':'))

    print(f"   {len(rows):,} ad-day rows")
    print(f"   Dimensions: {{p: {len(dimensions['portals'])}, c: {len(dimensions['categories'])}, ct: {len(dimensions['creative_types'])}, prod: {len(dimensions['products'])}}}")
    print(f"   Payload: {len(payload_json):,} chars")

    html = render_html(payload_json, since, until)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding='utf-8')
    print(f"\n✅ Wrote {out_path}  ({len(html):,} bytes)")
    conn.close()


if __name__ == '__main__':
    main()
