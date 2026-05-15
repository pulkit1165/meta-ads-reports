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


def fetch_active_campaign_budgets(conn, since: str, until: str):
    """Return campaign_id -> {daily_budget, name, status, portal} for all
    campaigns currently ACTIVE that had ads with spend in the period.

    Used by the Categories page Budget Allocation chart so the pie reflects
    configured spend ceilings, not just what was already spent.
    """
    rows = conn.execute('''
        SELECT c.campaign_id, c.portal, c.name, c.effective_status,
               COALESCE(c.daily_budget, 0) AS daily,
               COALESCE(c.lifetime_budget, 0) AS lifetime
        FROM meta_campaigns c
        WHERE c.campaign_id IN (
            SELECT DISTINCT campaign_id FROM meta_ads_daily
            WHERE date BETWEEN ? AND ? AND campaign_id IS NOT NULL
        )
    ''', (since, until)).fetchall()
    out = {}
    for r in rows:
        out[r[0]] = {
            'portal': r[1],
            'name': r[2] or '',
            'status': r[3] or '',
            'daily_budget': float(r[4] or 0),
            'lifetime_budget': float(r[5] or 0),
        }
    return out


def fetch_adsets(conn, since: str, until: str):
    """Adset metadata (name + audience inclusions/exclusions) for any adset
    that had spend in the period. Used by the Categories page drill-down
    to show camp structure including audience targeting.

    Returns an empty dict if the meta_adsets table hasn't been created
    yet (first deploy after the schema change, before db_init runs).
    """
    has_table = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='meta_adsets'"
    ).fetchone()
    if not has_table:
        return {}
    rows = conn.execute('''
        SELECT a.adset_id, a.portal, a.campaign_id, a.name,
               a.audiences_incl, a.audiences_excl, a.targeting_summary
        FROM meta_adsets a
        WHERE a.adset_id IN (
            SELECT DISTINCT adset_id FROM meta_ads_daily
            WHERE date BETWEEN ? AND ? AND adset_id IS NOT NULL
        )
    ''', (since, until)).fetchall()
    out = {}
    for r in rows:
        aid, portal, camp, name, incl, excl, summary = r
        out[aid] = {
            'portal': portal,
            'campaign_id': camp,
            'name': name,
            'audiences_incl': incl or '',
            'audiences_excl': excl or '',
            'targeting_summary': summary or '',
        }
    return out


def fetch_shopify_daily(conn, since: str, until: str):
    """Per-(portal, date) Shopify aggregates: real orders + real revenue.
    These are the GROUND TRUTH numbers — Meta's purchases/revenue from
    meta_ads_daily are pixel/CAPI-attributed and inflated by 2-3x in this
    account. Surfacing both lets the user see Pixel ROAS vs Real ROAS.

    Excludes cancelled orders. Uses created_at::date in the configured TZ
    (assumed IST since dates in meta_ads_daily are also IST-anchored).
    """
    rows = conn.execute('''
        SELECT
            portal,
            substr(created_at, 1, 10) AS date,
            COUNT(*) AS orders,
            SUM(COALESCE(total_price, 0)) AS revenue
        FROM shopify_orders
        WHERE substr(created_at, 1, 10) BETWEEN ? AND ?
          AND cancelled_at IS NULL
        GROUP BY portal, substr(created_at, 1, 10)
        ORDER BY portal, date
    ''', (since, until)).fetchall()
    out = []
    for portal, date, orders, revenue in rows:
        out.append({
            'portal':  portal,
            'date':    date,
            'orders':  int(orders or 0),
            'revenue': round(float(revenue or 0), 2),
        })
    return out


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
<title>NTN Analytics</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
* { margin:0; padding:0; box-sizing:border-box; }
body { font-family:-apple-system,'Segoe UI',Roboto,sans-serif; background:#f0f4fb; color:#1a1a2e; font-size:13px; }
a { color:#1a3d7c; text-decoration:none; }

/* Sidebar */
.sidebar { position:fixed; top:0; left:0; height:100vh; width:220px; background:#0d2145; color:#fff; padding:18px 0; overflow-y:auto; box-shadow:2px 0 8px rgba(0,0,0,.08); z-index:100; }
.sidebar-brand { padding:0 18px 14px; border-bottom:1px solid rgba(255,255,255,.1); margin-bottom:10px; }
.sidebar-brand h1 { font-size:15px; font-weight:800; }
.sidebar-brand p { font-size:10px; color:rgba(255,255,255,.5); margin-top:2px; }
.menu { list-style:none; }
.menu li a { display:flex; align-items:center; gap:10px; padding:10px 18px; color:rgba(255,255,255,.75); font-size:12px; font-weight:600; cursor:pointer; border-left:3px solid transparent; transition:all .12s; }
.menu li a:hover { background:rgba(255,255,255,.05); color:#fff; }
.menu li a.active { background:rgba(255,255,255,.08); color:#fff; border-left-color:#5b8def; font-weight:800; }
.menu-icon { font-size:14px; width:18px; text-align:center; }
.menu-section-lbl { padding:14px 18px 6px; font-size:9px; font-weight:800; text-transform:uppercase; letter-spacing:.6px; color:rgba(255,255,255,.4); }
.sidebar-footer { position:absolute; bottom:0; left:0; right:0; padding:14px 18px; border-top:1px solid rgba(255,255,255,.1); font-size:9px; color:rgba(255,255,255,.4); }
.sidebar-footer a { color:rgba(255,255,255,.6); }

/* Main wrapper */
.wrap { margin-left:220px; min-height:100vh; }

/* Top filter bar */
.topbar { background:#fff; padding:12px 20px; border-bottom:1px solid #dde3f0; position:sticky; top:0; z-index:50; box-shadow:0 1px 4px rgba(0,0,0,.04); }
.topbar-row { display:flex; gap:14px; flex-wrap:wrap; align-items:center; }
.ctrl-group { display:flex; flex-direction:column; gap:3px; }
.ctrl-lbl { font-size:9px; font-weight:700; text-transform:uppercase; letter-spacing:.5px; color:#6b7280; }
.ctrl-input, .ctrl-select { padding:5px 9px; border:1px solid #dde3f0; border-radius:6px; font-size:12px; min-width:120px; background:#fff; }
.preset-btns { display:flex; gap:4px; }
.preset-btn { padding:5px 10px; border:1px solid #dde3f0; background:#fff; border-radius:6px; font-size:11px; cursor:pointer; font-weight:600; }
.preset-btn.active { background:#1a3d7c; color:#fff; border-color:#1a3d7c; }
.btn-clear { background:#fff5f5; color:#a3260a; border:1px solid #fed7d7; padding:5px 10px; border-radius:6px; font-size:11px; cursor:pointer; font-weight:700; }
.multi-sel { display:flex; gap:4px; flex-wrap:wrap; max-width:280px; }
.chip { padding:3px 8px; border-radius:14px; background:#eef2ff; color:#1a3d7c; font-size:10px; font-weight:700; cursor:pointer; border:1px solid #dde3f0; }
.chip.active { background:#1a3d7c; color:#fff; border-color:#1a3d7c; }

/* Page content */
.page-area { padding:18px 20px; max-width:1700px; }
.page { display:none; }
.page.active { display:block; }
.page h2 { font-size:18px; font-weight:800; color:#0d2145; margin-bottom:5px; }
.page h2 .subtle { font-weight:400; font-size:12px; color:#6b7280; margin-left:8px; }
.page-intro { color:#6b7280; margin-bottom:14px; font-size:12px; }

/* KPI cards */
.kpi-strip { display:grid; grid-template-columns:repeat(auto-fit,minmax(170px,1fr)); gap:10px; margin-bottom:18px; }
.kpi-card { background:#fff; padding:12px 14px; border-radius:10px; border:1px solid #dde3f0; position:relative; overflow:hidden; }
.kpi-card.kpi-good::before { content:''; position:absolute; top:0; left:0; width:3px; height:100%; background:#0d6e3a; }
.kpi-card.kpi-warn::before { content:''; position:absolute; top:0; left:0; width:3px; height:100%; background:#a35a00; }
.kpi-card.kpi-bad::before  { content:''; position:absolute; top:0; left:0; width:3px; height:100%; background:#a3260a; }
.kpi-card.kpi-good .kpi-val { color:#0d6e3a; }
.kpi-card.kpi-warn .kpi-val { color:#a35a00; }
.kpi-card.kpi-bad  .kpi-val { color:#a3260a; }
.kpi-lbl { font-size:9px; text-transform:uppercase; letter-spacing:.5px; color:#6b7280; font-weight:700; }
.kpi-val { font-size:22px; font-weight:800; color:#0d2145; margin-top:2px; }
.kpi-sub { font-size:10px; color:#6b7280; margin-top:2px; }
.delta-up { color:#059669; font-weight:700; }
.delta-down { color:#dc2626; font-weight:700; }

/* Cards/sections within a page */
.card { background:#fff; border-radius:10px; padding:14px 16px; border:1px solid #dde3f0; margin-bottom:14px; }
.card h3 { font-size:13px; font-weight:800; color:#0d2145; margin-bottom:10px; padding-bottom:6px; border-bottom:1px solid #eef2ff; display:flex; align-items:center; justify-content:space-between; }
.card h3 .meta { font-weight:400; font-size:10px; color:#6b7280; }

.charts-row { display:grid; grid-template-columns:1fr 1fr; gap:14px; }
@media (max-width:1100px) { .charts-row { grid-template-columns:1fr; } }
.chart-wrap { position:relative; height:240px; }

/* Tables */
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

/* Heatmap success cells */
.sr-0 { background:#fef2f2; color:#7f1d1d; padding:2px 6px; border-radius:4px; }
.sr-low { background:#fef3c7; color:#78350f; padding:2px 6px; border-radius:4px; }
.sr-med { background:#dcfce7; color:#14532d; padding:2px 6px; border-radius:4px; }
.sr-hi { background:#bbf7d0; color:#065f46; font-weight:700; padding:2px 6px; border-radius:4px; }
</style>
</head>
<body>

<aside class="sidebar">
  <div class="sidebar-brand">
    <h1>📊 NTN Analytics</h1>
    <p>SQLite-backed · Hourly refresh</p>
  </div>

  <ul class="menu">
    <li class="menu-section-lbl">Performance</li>
    <li><a data-page="overview" class="active"><span class="menu-icon">📊</span>Overview</a></li>
    <li><a data-page="trends"><span class="menu-icon">📈</span>Trends</a></li>

    <li class="menu-section-lbl">Breakdowns</li>
    <li><a data-page="categories"><span class="menu-icon">📂</span>Categories</a></li>
    <li><a data-page="creatives"><span class="menu-icon">🎨</span>Creative Types</a></li>
    <li><a data-page="sentiments"><span class="menu-icon">💬</span>Sentiments</a></li>
    <li><a data-page="heatmap"><span class="menu-icon">🔥</span>Heatmap</a></li>

    <li class="menu-section-lbl">Drill-Down</li>
    <li><a data-page="prodbudget"><span class="menu-icon">💰</span>Product Budget</a></li>
    <li><a data-page="products"><span class="menu-icon">🛍️</span>Products</a></li>
    <li><a data-page="prodsuccess"><span class="menu-icon">🎯</span>Product Success</a></li>
    <li><a data-page="topads"><span class="menu-icon">🏆</span>Top Ads</a></li>
    <li><a data-page="bottomads"><span class="menu-icon">🥶</span>Bottom Ads</a></li>

    <li class="menu-section-lbl">Other Dashboards</li>
    <li><a href="/" target="_self"><span class="menu-icon">🏠</span>NTN Home</a></li>
    <li><a href="/today_live.html" target="_self"><span class="menu-icon">🔴</span>Today Live</a></li>
    <li><a href="/categories" target="_self"><span class="menu-icon">📁</span>Old Categories</a></li>
  </ul>

  <div class="sidebar-footer" id="sidebar-footer">Loading…</div>
</aside>

<div class="wrap">

  <!-- Top filter bar (sticky, applies to all pages) -->
  <div class="topbar">
    <div class="topbar-row">
      <div class="ctrl-group">
        <span class="ctrl-lbl">Date Range</span>
        <div class="preset-btns" id="preset-btns">
          <button class="preset-btn active" data-days="1">Today</button>
          <button class="preset-btn" data-days="3">3D</button>
          <button class="preset-btn" data-days="7">7D</button>
          <button class="preset-btn" data-days="14">14D</button>
          <button class="preset-btn" data-days="30">30D</button>
          <button class="preset-btn" data-days="all">All</button>
        </div>
      </div>
      <div class="ctrl-group">
        <span class="ctrl-lbl">From</span>
        <input type="date" class="ctrl-input" id="from-date">
      </div>
      <div class="ctrl-group">
        <span class="ctrl-lbl">To</span>
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
        <span class="ctrl-lbl">Creative</span>
        <div class="multi-sel" id="filter-creatives"></div>
      </div>
      <div class="ctrl-group" style="flex:1;min-width:280px;max-width:520px">
        <span class="ctrl-lbl">Product · families <span style="color:#9ca3af;font-weight:400">(type to search · click multiple)</span></span>
        <input type="text" class="ctrl-input" id="product-search" placeholder="🔎 type product name..." style="margin-bottom:4px;width:100%">
        <div class="multi-sel" id="filter-products" style="max-height:120px;overflow-y:auto"></div>
      </div>
      <div class="ctrl-group">
        <span class="ctrl-lbl">&nbsp;</span>
        <button class="btn-clear" id="btn-clear">Clear Filters</button>
      </div>
      <div class="ctrl-group" style="margin-left:auto">
        <span class="ctrl-lbl">Data refreshed</span>
        <div id="last-updated-pill" style="font-size:11px;font-weight:700;padding:5px 11px;border-radius:6px;white-space:nowrap;border:1px solid transparent"></div>
      </div>
      <div class="ctrl-group">
        <span class="ctrl-lbl">&nbsp;</span>
        <button id="btn-refresh-now" type="button" title="Force a fresh ingest + rebuild now (~10 min). Use this when the freshness pill is yellow/red."
                style="background:#1a3d7c;color:#fff;border:none;padding:6px 13px;border-radius:6px;font-size:11px;cursor:pointer;font-weight:700;white-space:nowrap">🔄 Refresh now</button>
      </div>
    </div>
  </div>

  <div class="page-area">

    <!-- ── PAGE: Overview ─────────────────────────────────────────── -->
    <section class="page active" id="page-overview">
      <h2>📊 Overview <span class="subtle" id="ov-meta"></span></h2>
      <p class="page-intro">Top-line KPIs and high-level breakdown for the selected period. Use sidebar to drill deeper.</p>

      <!-- Single Shopify-reality KPI strip. Pixel-attributed cards removed
           per user direction — Meta's pixel was inflating orders/revenue by
           ~1.8x and confusing primary read. Shopify Admin API is ground
           truth. The Spend / CPM / CTR / Active Ads cards stay because
           they're ad-mechanics, not attribution. -->
      <div id="range-pill" style="display:inline-block;background:#1a3d7c;color:#fff;font-size:13px;font-weight:700;padding:6px 14px;border-radius:18px;margin-bottom:12px"></div>
      <div class="kpi-strip" id="kpi-strip-shopify"></div>

      <div class="card">
        <h3>📈 Spend & ROAS by Day <span class="meta">spend-weighted, current filter</span></h3>
        <div class="charts-row">
          <div class="chart-wrap"><canvas id="chart-spend-rev"></canvas></div>
          <div class="chart-wrap"><canvas id="chart-roas"></canvas></div>
        </div>
      </div>

      <div class="card">
        <h3>📂 Top Categories</h3>
        <table id="tbl-cat-mini">
          <thead><tr><th>Category</th><th>Active Ads</th><th>Spend</th><th>Revenue</th><th>ROAS</th></tr></thead>
          <tbody></tbody>
        </table>
      </div>
    </section>

    <!-- ── PAGE: Trends ───────────────────────────────────────────── -->
    <section class="page" id="page-trends">
      <h2>📈 Trends Over Time</h2>
      <p class="page-intro">Time-series view of spend, revenue, ROAS, and orders.</p>

      <div class="card">
        <h3>Spend vs Revenue</h3>
        <div class="chart-wrap" style="height:300px"><canvas id="chart-trend-spend-rev"></canvas></div>
      </div>
      <div class="card">
        <h3>Daily ROAS</h3>
        <div class="chart-wrap" style="height:280px"><canvas id="chart-trend-roas"></canvas></div>
      </div>
      <div class="card">
        <h3>Daily Orders</h3>
        <div class="chart-wrap" style="height:280px"><canvas id="chart-trend-orders"></canvas></div>
      </div>
      <div class="card">
        <h3>Day-by-Day Table</h3>
        <table id="tbl-daily">
          <thead><tr><th>Date</th><th>Active Ads</th><th>Spend</th><th>Revenue</th><th>Orders</th><th>ROAS</th><th>CPM</th><th>CTR</th></tr></thead>
          <tbody></tbody>
        </table>
      </div>
    </section>

    <!-- ── PAGE: Categories ───────────────────────────────────────── -->
    <section class="page" id="page-categories">
      <h2>📂 Per-Category Breakdown</h2>
      <p class="page-intro">All metrics by product category. Click any column header to sort.</p>

      <div class="card">
        <h3>🥧 Budget Allocation by Category <span class="meta">slice = sum of ACTIVE campaign daily budgets · tooltip shows spent + ROAS</span></h3>
        <div class="chart-wrap" style="height:360px"><canvas id="chart-cat-pie"></canvas></div>
        <div id="cat-pie-empty" class="empty" style="display:none">No ACTIVE campaigns with configured daily budget in this period.</div>
      </div>

      <!-- Drill-down: appears only when a Product is picked or a single Category chip is selected.
           Shows per-creative-type breakdown (Paras/Motion/Partnership/Static/Other) and the
           per-campaign breakdown for the selected slice. -->
      <div class="card" id="card-drilldown" style="display:none">
        <h3>🔍 Drill-down: <span id="drill-title" style="color:#1a3d7c"></span></h3>
        <div id="drill-summary" style="margin:6px 0 14px;font-size:13px;color:#475569"></div>

        <h4 style="margin:14px 0 6px;font-size:12px;color:#1a3d7c;letter-spacing:.6px;text-transform:uppercase">Creative-type split</h4>
        <table id="tbl-drill-ct">
          <thead><tr>
            <th data-col="type" data-type="str">Creative Type</th>
            <th data-col="ads" data-type="num">Active Ads</th>
            <th data-col="spend" data-type="num">Spend</th>
            <th data-col="orders" data-type="num">Orders</th>
            <th data-col="revenue" data-type="num">Revenue</th>
            <th data-col="roas" data-type="num">ROAS</th>
          </tr></thead>
          <tbody id="drill-ct-tbody"></tbody>
        </table>

        <h4 style="margin:22px 0 6px;font-size:12px;color:#1a3d7c;letter-spacing:.6px;text-transform:uppercase">Ad-set structure · audience inclusions & exclusions · rolling ROAS</h4>
        <table id="tbl-drill-adset">
          <thead><tr>
            <th data-col="campaign" data-type="str">Campaign → Ad-set</th>
            <th data-col="portal" data-type="str">Portal</th>
            <th data-col="incl" data-type="str">Audience incl.</th>
            <th data-col="excl" data-type="str">Audience excl.</th>
            <th data-col="ads" data-type="num">Ads</th>
            <th data-col="spend" data-type="num">Spend (window)</th>
            <th data-col="orders" data-type="num">Orders</th>
            <th data-col="roas" data-type="num">ROAS (window)</th>
            <th data-col="roas_today" data-type="num">Live ROAS (today)</th>
            <th data-col="roas_3d" data-type="num">3D ROAS</th>
            <th data-col="roas_7d" data-type="num">7D ROAS</th>
          </tr></thead>
          <tbody id="drill-adset-tbody"></tbody>
        </table>
      </div>

      <div class="card">
        <h3>📊 Spend & ROAS by Category <span class="meta">aggregated over selected window</span></h3>
        <div class="chart-wrap" style="height:280px"><canvas id="chart-cat-bar"></canvas></div>
      </div>
      <div class="card" id="card-cat-trend-spend">
        <h3>📈 Daily Spend by Category <span class="meta">one line per category, selected window</span></h3>
        <div class="chart-wrap" style="height:320px"><canvas id="chart-cat-trend-spend"></canvas></div>
        <div id="cat-trend-spend-hint" class="empty" style="display:none">Pick <b>3D</b> or longer to see daily category trends — a single-day window has no line to draw.</div>
      </div>
      <div class="card" id="card-cat-trend-roas">
        <h3>🎯 Daily ROAS by Category <span class="meta">Meta-attributed (pixel)</span></h3>
        <div class="chart-wrap" style="height:320px"><canvas id="chart-cat-trend-roas"></canvas></div>
        <div id="cat-trend-roas-hint" class="empty" style="display:none">Pick <b>3D</b> or longer to see daily category trends.</div>
      </div>
      <div class="card">
        <h3>Full Table</h3>
        <table id="tbl-categories">
          <thead><tr>
            <th data-col="category" data-type="str">Category</th>
            <th data-col="active_ads" data-type="num">Active Ads</th>
            <th data-col="active_camps" data-type="num">Camps</th>
            <th data-col="spend" data-type="num">Spend (₹)</th>
            <th data-col="revenue" data-type="num">Revenue (₹)</th>
            <th data-col="purchases" data-type="num">Orders</th>
            <th data-col="roas" data-type="num">ROAS</th>
            <th data-col="cpm" data-type="num">CPM</th>
            <th data-col="ctr" data-type="num">CTR</th>
            <th data-col="atc_rate" data-type="num">ATC%</th>
            <th data-col="success_rate" data-type="num">Success% (7d)</th>
          </tr></thead>
          <tbody></tbody>
        </table>
      </div>
    </section>

    <!-- ── PAGE: Creative Types ───────────────────────────────────── -->
    <section class="page" id="page-creatives">
      <h2>🎨 Creative Types</h2>
      <p class="page-intro">Performance by creative origin: Paras, Wanda (AI), Partnership, Motion, Static, etc.</p>

      <div class="card">
        <h3>Comparison</h3>
        <div class="chart-wrap" style="height:280px"><canvas id="chart-ct-bar"></canvas></div>
      </div>
      <div class="card">
        <h3>Detail Table</h3>
        <table id="tbl-creatives">
          <thead><tr>
            <th data-col="creative_type" data-type="str">Type</th>
            <th data-col="active_ads" data-type="num">Ads</th>
            <th data-col="spend" data-type="num">Spend</th>
            <th data-col="revenue" data-type="num">Revenue</th>
            <th data-col="purchases" data-type="num">Orders</th>
            <th data-col="roas" data-type="num">ROAS</th>
            <th data-col="success_rate" data-type="num">Success% (7d)</th>
          </tr></thead>
          <tbody></tbody>
        </table>
      </div>
    </section>

    <!-- ── PAGE: Sentiments ───────────────────────────────────────── -->
    <section class="page" id="page-sentiments">
      <h2>💬 Sentiments × Creative Type</h2>
      <p class="page-intro">Sentiment codes (st1, st2…) come from <code>_st\d+_</code> tags in ad/campaign names. Update <code>sentiment_labels</code> table to set readable labels.</p>

      <div class="card">
        <h3>Sentiment Detail</h3>
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
    </section>

    <!-- ── PAGE: Heatmap ──────────────────────────────────────────── -->
    <section class="page" id="page-heatmap">
      <h2>🔥 Creative Type × Category Heatmap</h2>
      <p class="page-intro">Spend-weighted ROAS for each (category, creative) cell. Green ≥2.5x, amber 1.5-2.5x, red &lt;1.5x.</p>

      <div class="card" style="overflow-x:auto">
        <table id="tbl-heatmap"></table>
      </div>
    </section>

    <!-- ── PAGE: Product Budget ──────────────────────────────────── -->
    <section class="page" id="page-prodbudget">
      <h2>💰 Product Budget <span class="subtle">where Meta daily budget is allocated</span></h2>
      <p class="page-intro">Daily budget assigned at the Meta campaign level, rolled up by product. Only ACTIVE campaigns counted. Sorted descending — biggest budget first. Click any column header to re-sort.</p>

      <div class="kpi-strip" id="kpi-strip-prodbudget"></div>

      <div class="card">
        <h3>📊 Per-Product Daily Budget <span class="meta">rolled up across all active campaigns</span></h3>
        <table id="tbl-prodbudget">
          <thead><tr>
            <th data-col="product" data-type="str">Product</th>
            <th data-col="camps" data-type="num">Active Camps</th>
            <th data-col="daily_budget" data-type="num">Daily Budget</th>
            <th data-col="spend_today" data-type="num">Spend Today</th>
            <th data-col="utilization" data-type="num">Utilization %</th>
            <th data-col="spend_7d" data-type="num">Spend (7D)</th>
            <th data-col="roas_7d" data-type="num">7D ROAS</th>
            <th data-col="roas_window" data-type="num">ROAS (selected window)</th>
          </tr></thead>
          <tbody></tbody>
        </table>
      </div>

      <div class="card">
        <h3>📋 Per-Campaign Daily Budget <span class="meta">all active campaigns, top 100 by budget</span></h3>
        <table id="tbl-campbudget">
          <thead><tr>
            <th data-col="name" data-type="str">Campaign</th>
            <th data-col="product" data-type="str">Product</th>
            <th data-col="portal" data-type="str">Portal</th>
            <th data-col="daily_budget" data-type="num">Daily Budget</th>
            <th data-col="spend_today" data-type="num">Spend Today</th>
            <th data-col="utilization" data-type="num">Util %</th>
            <th data-col="spend_7d" data-type="num">Spend (7D)</th>
            <th data-col="roas_today" data-type="num">Live ROAS</th>
            <th data-col="roas_3d" data-type="num">3D ROAS</th>
            <th data-col="roas_7d" data-type="num">7D ROAS</th>
          </tr></thead>
          <tbody></tbody>
        </table>
      </div>
    </section>

    <!-- ── PAGE: Products ─────────────────────────────────────────── -->
    <section class="page" id="page-products">
      <h2>🛍️ Products <button class="btn-csv" onclick="exportCSV('products')">↓ CSV</button></h2>
      <p class="page-intro">Per-product performance with success-at-ROAS rates. Use the Product filter at top to drill into one.</p>

      <div class="card">
        <h3>Product Performance <span class="meta">success-at-roas % = of ads launched in period, fraction whose lifetime ROAS hit threshold</span></h3>
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
    </section>

    <!-- ── PAGE: Product Success Rate (campaign-level) ─────────────── -->
    <section class="page" id="page-prodsuccess">
      <h2>🎯 Product Success Rate <span class="subtle">campaign-level</span> <button class="btn-csv" onclick="exportCSV('prodsuccess')">↓ CSV</button></h2>
      <p class="page-intro">For each product: how many <strong>campaigns</strong> were published in the selected period, what their lifetime ROAS distribution looks like, and what % cleared each ROAS bar. Different from the Products page (which counts ads). Useful for "is this product worth scaling?" decisions.</p>

      <div class="card">
        <h3>Per-Product Campaign Success <span class="meta">campaigns w/ ≥ ₹500 spend in period · ROAS = period revenue / period spend per campaign</span></h3>
        <table id="tbl-prodsuccess">
          <thead><tr>
            <th data-col="product" data-type="str">Product</th>
            <th data-col="category" data-type="str">Cat</th>
            <th data-col="campaigns" data-type="num">Camps Pub.</th>
            <th data-col="spend" data-type="num">Spend</th>
            <th data-col="revenue" data-type="num">Revenue</th>
            <th data-col="orders" data-type="num">Orders</th>
            <th data-col="roas" data-type="num">Agg ROAS</th>
            <th data-col="best_roas" data-type="num">Best Camp</th>
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
    </section>

    <!-- ── PAGE: Top Ads ──────────────────────────────────────────── -->
    <section class="page" id="page-topads">
      <h2>🏆 Top Ads <button class="btn-csv" onclick="exportCSV('topads')">↓ CSV</button></h2>
      <p class="page-intro">Top 50 by ROAS. Min ₹2K spend filter to skip flukes.</p>
      <div class="card">
        <table id="tbl-topads">
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
    </section>

    <!-- ── PAGE: Bottom Ads ───────────────────────────────────────── -->
    <section class="page" id="page-bottomads">
      <h2>🥶 Bottom Ads <span class="subtle">kill candidates</span> <button class="btn-csv" onclick="exportCSV('bottomads')">↓ CSV</button></h2>
      <p class="page-intro">Worst 50 by ROAS, min ₹2K spend (avoids surfacing tiny test ads). These are kill candidates per your same-day kill protocol.</p>
      <div class="card">
        <table id="tbl-bottomads">
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
    </section>

  </div>
</div>

<script>
const PAYLOAD = __PAYLOAD__;
const RAW = PAYLOAD.rows;
const DIM = PAYLOAD.dimensions;
const FRESH = PAYLOAD.freshness;
document.getElementById('sidebar-footer').innerHTML =
  `Built ${PAYLOAD.updated_at.slice(0, 16)}<br>${RAW.length.toLocaleString()} ad-day rows · ${DIM.products.length} products · ${DIM.categories.length} categories`;

// Top-bar freshness pill — sticky and visible from any page. Color-coded
// by age so a stalled cron is obvious without checking GHA.
//   green  ≤ 75 min  (ingest runs hourly, ~8 min latency)
//   amber  75–180 min
//   red    > 180 min — almost certainly broken pipeline
function renderLastUpdated() {
  const t   = new Date(PAYLOAD.updated_at);
  const now = new Date();
  const diffMin = Math.max(0, Math.floor((now - t) / 60000));
  let rel;
  if (diffMin < 1)         rel = 'just now';
  else if (diffMin < 60)   rel = `${diffMin}m ago`;
  else if (diffMin < 1440) rel = `${Math.floor(diffMin/60)}h ${diffMin%60}m ago`;
  else                     rel = `${Math.floor(diffMin/1440)}d ago`;
  const istTime = t.toLocaleString('en-IN', {
    timeZone: 'Asia/Kolkata',
    hour: '2-digit', minute: '2-digit', hour12: false,
    day: '2-digit', month: 'short',
  });
  const pill = document.getElementById('last-updated-pill');
  let dot, bg, bd, fg;
  if (diffMin > 180) {
    dot = '🔴'; bg = '#fef2f2'; bd = '#fca5a5'; fg = '#991b1b';
  } else if (diffMin > 75) {
    dot = '🟡'; bg = '#fef3c7'; bd = '#fcd34d'; fg = '#92400e';
  } else {
    dot = '🟢'; bg = '#e6f7ec'; bd = '#b8e6c8'; fg = '#0d6e3a';
  }
  pill.style.background = bg;
  pill.style.borderColor = bd;
  pill.style.color = fg;
  pill.textContent = `${dot} ${rel} · ${istTime} IST`;
  pill.title = `Dashboard payload built at ${PAYLOAD.updated_at}\nv2-ingest runs hourly via Cloudflare Worker + GHA fallback`;
}
renderLastUpdated();
setInterval(renderLastUpdated, 30000);

// "Refresh now" button — fires the Cloudflare Worker pings that dispatch
// v2-ingest immediately, then today-live ~8 min later (after ingest finishes).
// User can also navigate away — both Worker pings are fire-and-forget, and
// the regular hourly schedule covers the gap if anything is missed.
const REFRESH_WORKER = 'https://meta-ads-cron-pinger.pulkit-studdmuffyn.workers.dev';
document.getElementById('btn-refresh-now').addEventListener('click', async (e) => {
  const btn = e.currentTarget;
  const origText = btn.textContent;
  btn.disabled = true; btn.style.opacity = '0.65';
  btn.textContent = '⏳ Queuing ingest...';
  try {
    const r = await fetch(`${REFRESH_WORKER}/ping-ingest`, { mode: 'cors' });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    btn.textContent = '✓ Refresh queued · ~10 min';
    btn.style.background = '#059669';
    // Auto-trigger deploy after ingest should be done (~8 min)
    setTimeout(async () => {
      try { await fetch(`${REFRESH_WORKER}/ping-deploy`, { mode: 'cors' }); } catch (_) {}
      btn.textContent = '🚀 Rebuild firing... reload in 5 min';
    }, 8 * 60 * 1000);
    // Re-enable + suggest reload after full cycle
    setTimeout(() => {
      btn.textContent = '🔁 Reload page for new data';
      btn.style.background = '#1a3d7c';
      btn.disabled = false; btn.style.opacity = '1';
      btn.onclick = () => location.reload();
    }, 13 * 60 * 1000);
  } catch (err) {
    btn.textContent = `✗ ${err.message} (retry)`;
    btn.style.background = '#dc2626';
    setTimeout(() => {
      btn.textContent = origText;
      btn.style.background = '#1a3d7c';
      btn.disabled = false; btn.style.opacity = '1';
    }, 4000);
  }
});

// ── Filter state ─────────────────────────────────────────────────────────
const F = {
  fromDate: PAYLOAD.since,
  toDate:   PAYLOAD.until,
  portals:  new Set(),
  categories: new Set(),
  creative_types: new Set(),
  product_families: new Set(),    // multi-select; was F.product (string)
};

// Collapse SKU variants into a "product family" so 'AM/PM Booster Kit [165ml]',
// 'AM PM Pigmentation Combo', 'AM PM 180ml' all bucket under 'am pm'. Used
// by the product filter and by drill-down lookups. Leaves auto-derived
// slugs (~prefixed) intact — they're already short identifiers.
const _PRODUCT_FAMILY_STOP = new Set([
  'the','a','an','of','for','with','and','&',
  'nuskhe','by','paras','studd','muffyn','sm','sml','nbp','ntn',
  'combo','combo:','kit','pack','set','bundle','bottle','jar',
]);
function productFamily(name) {
  if (!name) return '(no product tag)';
  if (name[0] === '~') {
    const slug = name.slice(1).split('_').slice(0, 2).join(' ');
    return '~' + slug;
  }
  let n = name.replace(/\s*[\(\[\{][^\)\]\}]*[\)\]\}]\s*/g, ' '); // strip (...)/[...]
  n = n.toLowerCase().replace(/[\/\-,]/g, ' ').replace(/\s+/g, ' ').trim();
  const tokens = n.split(' ').filter(t => t);
  const significant = tokens.filter(t => !_PRODUCT_FAMILY_STOP.has(t) && t.length > 1);
  const pick = significant.length >= 2 ? significant.slice(0, 2)
             : significant.length === 1 ? significant
             : tokens.slice(0, 2);
  return pick.join(' ') || name;
}

const fmt = {
  inr:  n => n == null ? '—' : '₹' + Math.round(n).toLocaleString('en-IN'),
  num:  n => n == null ? '—' : Math.round(n).toLocaleString('en-IN'),
  num1: n => n == null ? '—' : Number(n).toFixed(1),
  pct:  n => n == null ? '—' : Number(n).toFixed(1) + '%',
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

// ── Filter UI population ────────────────────────────────────────────────
function buildChips(containerId, values, set) {
  const c = document.getElementById(containerId);
  c.innerHTML = '';
  values.forEach(v => {
    const chip = document.createElement('span');
    chip.className = 'chip';
    chip.textContent = v;
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

// Build product-family chips with type-to-search.
// Each chip = one family; count = SKUs grouped into it.
const _famCount = new Map();
DIM.products.forEach(p => {
  const fam = productFamily(p);
  _famCount.set(fam, (_famCount.get(fam) || 0) + 1);
});
const _famList = [...new Set(DIM.products.map(productFamily))].sort();
const prodChipContainer = document.getElementById('filter-products');
function renderProductChips(filterText) {
  const q = (filterText || '').trim().toLowerCase();
  prodChipContainer.innerHTML = '';
  let shown = 0;
  for (const fam of _famList) {
    if (q && !fam.toLowerCase().includes(q)) continue;
    const chip = document.createElement('span');
    chip.className = 'chip';
    if (F.product_families.has(fam)) chip.classList.add('active');
    const cnt = _famCount.get(fam) || 1;
    chip.textContent = cnt > 1 ? `${fam} (${cnt})` : fam;
    chip.title = `${fam} — ${cnt} SKU variant${cnt > 1 ? 's' : ''}`;
    chip.addEventListener('click', () => {
      if (F.product_families.has(fam)) { F.product_families.delete(fam); chip.classList.remove('active'); }
      else { F.product_families.add(fam); chip.classList.add('active'); }
      apply();
    });
    prodChipContainer.appendChild(chip);
    shown++;
    if (shown >= 200) break;   // cap so search-of-empty doesn't render 1000 chips
  }
  if (shown === 0) {
    prodChipContainer.innerHTML = '<span class="subtle" style="padding:6px">no products match</span>';
  }
}
renderProductChips('');
document.getElementById('product-search').addEventListener('input', e => {
  renderProductChips(e.target.value);
});

document.getElementById('from-date').value = F.fromDate;
document.getElementById('to-date').value   = F.toDate;
document.getElementById('from-date').addEventListener('change', e => { F.fromDate = e.target.value; clearActivePreset(); apply(); });
document.getElementById('to-date').addEventListener('change',   e => { F.toDate   = e.target.value; clearActivePreset(); apply(); });

// Format a Date as YYYY-MM-DD in LOCAL time. Using `.toISOString().slice(0,10)`
// is a TZ landmine — it converts to UTC first, which in IST (+05:30) bumps
// the date back by one for any date built from a "YYYY-MM-DDT00:00:00" string.
// That bug made "Today" produce a 2-day range and 7D produce 8 days etc.
const fmtLocalDate = d =>
  `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;

document.querySelectorAll('.preset-btn').forEach(b => {
  b.addEventListener('click', () => {
    document.querySelectorAll('.preset-btn').forEach(x => x.classList.remove('active'));
    b.classList.add('active');
    const days = b.dataset.days;
    if (days === 'all') {
      F.fromDate = PAYLOAD.since; F.toDate = PAYLOAD.until;
    } else {
      const end = new Date(PAYLOAD.until + 'T00:00:00');
      const n = parseInt(days, 10);
      const start = new Date(end); start.setDate(end.getDate() - (n - 1));
      F.fromDate = fmtLocalDate(start);
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
  F.portals.clear(); F.categories.clear(); F.creative_types.clear(); F.product_families.clear();
  // Clear search box + re-render full chip list
  const ps = document.getElementById('product-search'); if (ps) ps.value = '';
  renderProductChips('');
  document.querySelectorAll('.chip').forEach(c => c.classList.remove('active'));
  prodSel.value = '';
  apply();
});

// ── Sidebar routing ─────────────────────────────────────────────────────
// Single switchPage() function — both clicks and hashchange call this so
// DOM state is always consistent. Earlier version used `location.hash = page`
// inside the click handler, which fired `hashchange`, which called .click()
// on the same link, which re-entered the handler. In jsdom this caused
// the .active class to bounce; in Chrome it MAY work but is fragile.
// Switching to `history.replaceState` avoids the hashchange entirely.
function switchPage(page) {
  if (!page) page = 'overview';
  const link = document.querySelector('.menu a[data-page="' + page + '"]');
  const section = document.getElementById('page-' + page);
  if (!link || !section) return;
  document.querySelectorAll('.menu a').forEach(x => x.classList.remove('active'));
  link.classList.add('active');
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  section.classList.add('active');
  // Update URL hash without firing hashchange (no re-entry)
  if (location.hash !== '#' + page) {
    history.replaceState(null, '', '#' + page);
  }
  // Defer apply() so the browser computes layout for the just-shown
  // .page section before Chart.js measures its canvases. requestAnimation
  // Frame alone is sometimes not enough in Chrome — the canvas can still
  // read 0x0 on first paint. Belt-and-braces:
  //   1. rAF to wait for layout
  //   2. apply() to render charts
  //   3. window resize event so Chart.js (responsive:true) re-measures
  //      and redraws if it got the wrong size on instantiation
  const doApply = () => {
    apply();
    // Force Chart.js to re-measure any responsive charts that may have
    // been built on a 0x0 canvas. Tiny setTimeout so resize fires after
    // chart instances are in place.
    setTimeout(() => {
      try { window.dispatchEvent(new Event('resize')); } catch (_) {}
    }, 50);
  };
  if (typeof requestAnimationFrame === 'function') {
    requestAnimationFrame(doApply);
  } else {
    doApply();
  }
}

document.querySelectorAll('.menu a[data-page]').forEach(a => {
  a.addEventListener('click', (e) => {
    e.preventDefault();
    switchPage(a.dataset.page);
  });
});

function activatePageFromHash() {
  const hash = location.hash.replace('#', '') || 'overview';
  switchPage(hash);
}
window.addEventListener('hashchange', activatePageFromHash);

// ── Filter logic ────────────────────────────────────────────────────────
function applyFilters(rows) {
  return rows.filter(r => {
    if (r.date < F.fromDate || r.date > F.toDate) return false;
    if (F.portals.size && !F.portals.has(r.portal)) return false;
    if (F.categories.size && !F.categories.has(r.category)) return false;
    if (F.creative_types.size && !F.creative_types.has(r.creative_type)) return false;
    if (F.product_families.size && !F.product_families.has(productFamily(r.product))) return false;
    return true;
  });
}

function getCompareSet() {
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
    if (F.product_families.size && !F.product_families.has(productFamily(r.product))) return false;
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
    key: a.key,
    active_ads: a.active_ads.size,
    active_camps: a.active_camps.size,
    spend: a.spend, revenue: a.revenue, purchases: a.purchases,
    impressions: a.impressions, clicks: a.clicks,
    atc: a.atc, lpv: a.lpv,
    roas: a.spend > 0 ? a.revenue / a.spend : 0,
    cpm:  a.impressions > 0 ? (a.spend / a.impressions) * 1000 : 0,
    ctr:  a.impressions > 0 ? (a.clicks / a.impressions) * 100 : 0,
    atc_rate: a.clicks > 0 ? (a.atc / a.clicks) * 100 : 0,
  }));
}

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

// Per-date Shopify totals respecting current date+portal filter.
// (Shopify can't be filtered by category/creative/product — those filters
// are silently ignored here and the daily totals stay portal-level.)
function shopifyTimeSeries() {
  const by = new Map();
  for (const r of (PAYLOAD.shopify_daily || [])) {
    if (r.date < F.fromDate || r.date > F.toDate) continue;
    if (F.portals.size && !F.portals.has(r.portal)) continue;
    if (!by.has(r.date)) by.set(r.date, { orders: 0, revenue: 0 });
    const b = by.get(r.date);
    b.orders  += r.orders;
    b.revenue += r.revenue;
  }
  return by;
}

function timeSeries(rows) {
  // Meta-side mechanics (spend, impressions, clicks, ad count) come from
  // the filtered ad-day rows. Revenue / orders / ROAS come from Shopify
  // (ground truth) — Meta pixel attribution is over-reporting ~1.8x.
  const by = new Map();
  for (const r of rows) {
    if (!by.has(r.date)) by.set(r.date, { spend:0, ads: new Set(), impressions:0, clicks:0 });
    const b = by.get(r.date);
    b.spend       += r.spend       || 0;
    b.impressions += r.impressions || 0;
    b.clicks      += r.clicks      || 0;
    b.ads.add(r.ad_id);
  }
  const shop = shopifyTimeSeries();
  const dates = new Set([...by.keys(), ...shop.keys()]);
  const sorted = [...dates].sort();
  return sorted.map(d => {
    const v = by.get(d)   || { spend:0, ads: new Set(), impressions:0, clicks:0 };
    const s = shop.get(d) || { orders: 0, revenue: 0 };
    return {
      date: d,
      active_ads: v.ads.size,
      spend: Math.round(v.spend),
      revenue: Math.round(s.revenue),  // Shopify ground truth
      orders: s.orders,                 // Shopify ground truth
      roas: v.spend > 0 ? +(s.revenue / v.spend).toFixed(2) : 0,  // Real ROAS
      cpm: v.impressions > 0 ? +((v.spend / v.impressions) * 1000).toFixed(2) : 0,
      ctr: v.impressions > 0 ? +((v.clicks / v.impressions) * 100).toFixed(2) : 0,
    };
  });
}

// ── Sort state per table ────────────────────────────────────────────────
const sortState = {};
function setupSort(tableId, defaultCol, defaultDir = 'desc') {
  sortState[tableId] = { col: defaultCol, dir: defaultDir };
  document.querySelectorAll(`#${tableId} thead th`).forEach(th => {
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
  const st = sortState[tableId];
  if (!st) return rows;
  const { col, dir } = st;
  const factor = dir === 'asc' ? 1 : -1;
  return rows.slice().sort((a, b) => {
    const va = a[col], vb = b[col];
    if (va == null) return 1; if (vb == null) return -1;
    if (typeof va === 'number') return (va - vb) * factor;
    return String(va).localeCompare(String(vb)) * factor;
  });
}

// ── Chart pool ──────────────────────────────────────────────────────────
let charts = {};
function destroyChart(id) { if (charts[id]) { charts[id].destroy(); delete charts[id]; } }

function lineChart(canvasId, ts, datasets, opts = {}) {
  destroyChart(canvasId);
  const el = document.getElementById(canvasId);
  if (!el) return;
  charts[canvasId] = new Chart(el, {
    type: 'line',
    data: { labels: ts.map(t => t.date.slice(5)), datasets },
    options: { responsive:true, maintainAspectRatio:false, ...opts },
  });
}
function barChart(canvasId, labels, datasets, opts = {}) {
  destroyChart(canvasId);
  const el = document.getElementById(canvasId);
  if (!el) return;
  // Chart.js v4 requires `type` at the TOP level of config — putting it
  // inside `options` is silently ignored. Pull it out of opts; default to
  // 'bar' since this helper is mostly used for bar charts.
  const { type, ...restOpts } = opts;
  charts[canvasId] = new Chart(el, {
    type: type || 'bar',
    data: { labels, datasets },
    options: { responsive:true, maintainAspectRatio:false, ...restOpts },
  });
}

function pieChart(canvasId, labels, datasets, opts = {}) {
  destroyChart(canvasId);
  const el = document.getElementById(canvasId);
  if (!el) return;
  charts[canvasId] = new Chart(el, {
    type: 'pie',
    data: { labels, datasets },
    options: { responsive:true, maintainAspectRatio:false, ...opts },
  });
}

// ── Page renderers ──────────────────────────────────────────────────────
// Compute Shopify (real) totals from PAYLOAD.shopify_daily, scoped to the
// current date filter and any selected portals. We can't filter Shopify by
// category/creative/product (no per-line attribution) — so the Shopify strip
// always shows portal-level totals; the user can still narrow by date+portal.
function aggregateShopify() {
  const shopify = (PAYLOAD.shopify_daily || []);
  let orders = 0, revenue = 0;
  for (const r of shopify) {
    if (r.date < F.fromDate || r.date > F.toDate) continue;
    if (F.portals.size && !F.portals.has(r.portal)) continue;
    orders  += r.orders;
    revenue += r.revenue;
  }
  return { orders, revenue };
}

function renderOverview(rows, prevRows) {
  // Meta-attributed (Pixel) KPI strip
  const a = aggregate(rows)[0] || { spend:0, revenue:0, purchases:0, active_ads:0, active_camps:0, roas:0, cpm:0, ctr:0 };
  const p = aggregate(prevRows)[0] || { spend:0, revenue:0, purchases:0, roas:0 };
  document.getElementById('ov-meta').textContent = `· ${F.fromDate} → ${F.toDate}`;

  // Range pill — make it unmistakable whether KPIs are a single day or a sum
  const fromD = new Date(F.fromDate + 'T00:00:00');
  const toD   = new Date(F.toDate   + 'T00:00:00');
  const ndays = Math.round((toD - fromD) / 86400000) + 1;
  const todayStr = PAYLOAD.until;
  let rangeLabel;
  if (F.fromDate === F.toDate) {
    rangeLabel = F.fromDate === todayStr
      ? `📅 TODAY (${F.fromDate}) — live, refreshes hourly`
      : `📅 ${F.fromDate} (single day)`;
  } else {
    rangeLabel = `📅 ${F.fromDate} → ${F.toDate} (${ndays} days, totals are SUMS over the window)`;
  }
  document.getElementById('range-pill').textContent = rangeLabel;

  // ── Shopify-reality KPI strip (single source of truth for orders/revenue/ROAS)
  // Pixel-attributed Meta numbers (purchases/revenue/ROAS) removed — they
  // inflate by ~1.8x and were causing the dashboard to read as "wrong".
  // Ad-mechanics metrics that don't depend on attribution (Active Ads, CPM,
  // CTR, Success Rate) stay alongside Shopify orders/revenue.
  const shop = aggregateShopify();
  const realROAS  = a.spend > 0 ? (shop.revenue / a.spend) : 0;
  const filterNote = (F.categories.size || F.creative_types.size || F.product_families.size)
    ? "⚠ Shopify can't filter by category/creative — portal-level total"
    : 'all portals · all orders';

  const cards = [
    { l:'Active Ads',     v: fmt.num(a.active_ads),  s: a.active_camps + ' campaigns' },
    { l:'Meta Spend',     v: fmt.inr(a.spend),       s: fmt.delta(a.spend, p.spend) + ' vs prev' },
    { l:'Real Orders',    v: fmt.num(shop.orders),   s: filterNote },
    { l:'Real Revenue',   v: fmt.inr(shop.revenue),  s: filterNote },
    { l:'ROAS',           v: fmt.roas(realROAS),     s: 'Shopify rev ÷ Meta spend',  cls: realROAS >= 2 ? 'good' : (realROAS >= 1.5 ? 'warn' : 'bad') },
    { l:'CPM',            v: fmt.inr(a.cpm),         s: 'cost / 1k impr' },
    { l:'CTR',            v: fmt.pct(a.ctr),         s: 'click-through' },
    { l:'Success Rate (7d)', v: (() => { const s = successRate(rows); return s == null ? '—' : fmt.pct(s); })(), s: 'launched in period · survived 7d' },
  ];
  document.getElementById('kpi-strip-shopify').innerHTML = cards.map(c =>
    `<div class="kpi-card${c.cls ? ' kpi-' + c.cls : ''}"><div class="kpi-lbl">${c.l}</div>` +
    `<div class="kpi-val">${c.v}</div><div class="kpi-sub">${c.s}</div></div>`
  ).join('');

  // Mini charts
  if (document.getElementById('page-overview').classList.contains('active')) {
    const ts = timeSeries(rows);
    lineChart('chart-spend-rev', ts, [
      { label:'Spend',   data: ts.map(t => t.spend),   borderColor:'#1a3d7c', backgroundColor:'#1a3d7c33', fill:true, tension:.25 },
      { label:'Revenue', data: ts.map(t => t.revenue), borderColor:'#059669', backgroundColor:'#05966933', fill:true, tension:.25 },
    ], { plugins:{ title:{ display:true, text:'Spend vs Revenue (₹)'} },
         scales:{ y:{ ticks:{ callback:v => '₹' + (v >= 100000 ? (v/100000).toFixed(1)+'L' : v) } } } });
    lineChart('chart-roas', ts, [
      { label:'ROAS', data: ts.map(t => t.roas), borderColor:'#d97706', backgroundColor:'#d9770633', fill:true, tension:.25 },
    ], { plugins:{ title:{ display:true, text:'Daily ROAS'} } });
  }

  // Mini category table
  const cats = aggregate(rows, 'category').filter(c => c.key).sort((a, b) => b.spend - a.spend).slice(0, 8);
  document.querySelector('#tbl-cat-mini tbody').innerHTML = cats.map(c =>
    `<tr><td><strong>${c.key}</strong></td><td>${fmt.num(c.active_ads)}</td>` +
    `<td>${fmt.inr(c.spend)}</td><td>${fmt.inr(c.revenue)}</td><td>${fmt.roas(c.roas)}</td></tr>`
  ).join('') || '<tr><td colspan="5" class="empty">No data.</td></tr>';
}

function renderTrends(rows) {
  if (!document.getElementById('page-trends').classList.contains('active')) return;
  const ts = timeSeries(rows);
  lineChart('chart-trend-spend-rev', ts, [
    { label:'Spend',   data: ts.map(t => t.spend),   borderColor:'#1a3d7c', backgroundColor:'#1a3d7c33', fill:true, tension:.25 },
    { label:'Revenue', data: ts.map(t => t.revenue), borderColor:'#059669', backgroundColor:'#05966933', fill:true, tension:.25 },
  ], { scales:{ y:{ ticks:{ callback:v => '₹' + (v >= 100000 ? (v/100000).toFixed(1)+'L' : v) } } } });
  lineChart('chart-trend-roas', ts, [
    { label:'ROAS', data: ts.map(t => t.roas), borderColor:'#d97706', backgroundColor:'#d9770633', fill:true, tension:.25 },
  ]);
  barChart('chart-trend-orders', ts.map(t => t.date.slice(5)), [
    { label:'Orders', data: ts.map(t => t.orders), backgroundColor:'#1a3d7c' },
  ], { type: 'bar' });

  // Daily table
  document.querySelector('#tbl-daily tbody').innerHTML = ts.slice().reverse().map(t =>
    `<tr><td>${t.date}</td><td>${fmt.num(t.active_ads)}</td>` +
    `<td>${fmt.inr(t.spend)}</td><td>${fmt.inr(t.revenue)}</td><td>${fmt.num(t.orders)}</td>` +
    `<td>${fmt.roas(t.roas)}</td><td>${fmt.inr(t.cpm)}</td><td>${fmt.num1(t.ctr)}</td></tr>`
  ).join('') || '<tr><td colspan="8" class="empty">No data.</td></tr>';
}

// Stable palette for category lines — same color per category across charts.
const CAT_COLORS = [
  '#1a3d7c', '#059669', '#d97706', '#dc2626', '#7c3aed',
  '#0891b2', '#db2777', '#65a30d', '#ea580c', '#475569',
  '#a16207', '#1e40af',
];
const catColor = (() => {
  const map = new Map();
  return name => {
    if (!map.has(name)) map.set(name, CAT_COLORS[map.size % CAT_COLORS.length]);
    return map.get(name);
  };
})();

// Build per-(category, date) buckets from the current filtered rows. Used
// for the two multi-line trend charts on the Categories page.
function categoryTimeSeries(rows) {
  const byCat = new Map();        // category -> Map(date -> {spend, revenue})
  const allDates = new Set();
  for (const r of rows) {
    if (!r.category) continue;
    if (!byCat.has(r.category)) byCat.set(r.category, new Map());
    const m = byCat.get(r.category);
    if (!m.has(r.date)) m.set(r.date, { spend: 0, revenue: 0 });
    const b = m.get(r.date);
    b.spend   += r.spend   || 0;
    b.revenue += r.revenue || 0;
    allDates.add(r.date);
  }
  const dates = [...allDates].sort();
  // Build datasets — dense (zero-fill missing dates so lines stay continuous)
  const buildDataset = (metric) => [...byCat.entries()]
    // Sort by total spend desc so legend matches table order
    .map(([cat, m]) => {
      const total = [...m.values()].reduce((s, v) => s + v.spend, 0);
      return { cat, m, total };
    })
    .sort((a, b) => b.total - a.total)
    .map(({ cat, m }) => ({
      label: cat,
      data: dates.map(d => {
        const v = m.get(d);
        if (!v) return 0;
        if (metric === 'spend')   return Math.round(v.spend);
        if (metric === 'roas')    return v.spend > 0 ? +(v.revenue / v.spend).toFixed(2) : 0;
        return 0;
      }),
      borderColor:     catColor(cat),
      backgroundColor: catColor(cat) + '22',
      tension: .25,
      fill: false,
      pointRadius: 2,
    }));
  return { dates, spend: buildDataset('spend'), roas: buildDataset('roas') };
}

function renderDrillDown(rows) {
  // Show only when the operator is drilling into something specific:
  // a product, OR exactly one category chip, OR exactly one creative chip.
  // Otherwise the tables would just repeat the page-level totals.
  const card = document.getElementById('card-drilldown');
  if (!card) return;
  const prodFams  = [...F.product_families];
  const singleCat = F.categories.size === 1 ? [...F.categories][0] : null;
  const singleCt  = F.creative_types.size === 1 ? [...F.creative_types][0] : null;
  if (prodFams.length === 0 && !singleCat && !singleCt) {
    card.style.display = 'none';
    return;
  }
  card.style.display = '';

  const titleParts = [];
  if (prodFams.length) titleParts.push(`Product${prodFams.length>1?'s':''} = ${prodFams.join(', ')}`);
  if (singleCat) titleParts.push(`Category = ${singleCat}`);
  if (singleCt)  titleParts.push(`Creative = ${singleCt}`);
  document.getElementById('drill-title').textContent = titleParts.join(' · ');

  // Aggregate the filtered rows by creative_type and by adset_id.
  // Adset is the level where audience targeting lives in Meta, so the
  // structure table is grouped per-adset (campaigns appear in the first
  // column as "Campaign → Ad-set").
  const byCT = new Map();
  const byAdset = new Map();
  const allAds = new Set();
  let totalSpend = 0, totalRev = 0, totalPur = 0;
  for (const r of rows) {
    allAds.add(r.ad_id);
    totalSpend += r.spend || 0;
    totalRev   += r.revenue || 0;
    totalPur   += r.purchases || 0;

    const ct = r.creative_type || 'Other';
    if (!byCT.has(ct)) byCT.set(ct, { ads: new Set(), spend: 0, revenue: 0, purchases: 0 });
    const x = byCT.get(ct);
    x.ads.add(r.ad_id);
    x.spend     += r.spend     || 0;
    x.revenue   += r.revenue   || 0;
    x.purchases += r.purchases || 0;

    if (r.adset_id) {
      if (!byAdset.has(r.adset_id)) byAdset.set(r.adset_id, {
        campaign_name: r.campaign_name || '(unnamed)',
        adset_name:    r.adset_name    || '(no adset name)',
        portal:        r.portal        || '',
        ads: new Set(), spend: 0, revenue: 0, purchases: 0,
      });
      const y = byAdset.get(r.adset_id);
      y.ads.add(r.ad_id);
      y.spend     += r.spend     || 0;
      y.revenue   += r.revenue   || 0;
      y.purchases += r.purchases || 0;
    }
  }

  // Summary line above the two tables
  const realRoas = totalSpend > 0 ? totalRev / totalSpend : 0;
  document.getElementById('drill-summary').innerHTML =
    `<b>${allAds.size}</b> active ads · ` +
    `Spend <b>${fmt.inr(totalSpend)}</b> · ` +
    `Pixel Orders <b>${fmt.num(totalPur)}</b> · ` +
    `Pixel Revenue <b>${fmt.inr(totalRev)}</b> · ` +
    `Pixel ROAS <b>${fmt.roas(realRoas)}</b>` +
    `<div class="subtle" style="margin-top:4px">Note: orders / revenue / ROAS below are Meta-pixel-attributed (Shopify can't filter by product/creative).</div>`;

  // Creative-type breakdown
  const ctRows = [...byCT.entries()].map(([type, v]) => ({
    type, ads: v.ads.size, spend: v.spend, revenue: v.revenue, orders: v.purchases,
    roas: v.spend > 0 ? v.revenue / v.spend : 0,
  })).sort((a, b) => b.spend - a.spend);
  document.getElementById('drill-ct-tbody').innerHTML = ctRows.map(r =>
    `<tr><td><strong>${r.type}</strong></td>` +
    `<td>${fmt.num(r.ads)}</td>` +
    `<td>${fmt.inr(r.spend)}</td>` +
    `<td>${fmt.num(r.orders)}</td>` +
    `<td>${fmt.inr(r.revenue)}</td>` +
    `<td>${fmt.roas(r.roas)}</td></tr>`
  ).join('') || '<tr><td colspan="6" class="empty">No data for current filter.</td></tr>';

  // Adset breakdown (top 50 by spend) — joined with PAYLOAD.adsets so we
  // get audience inclusions / exclusions per adset.
  // Rolling ROAS columns (Live / 3D / 7D) need data outside the operator's
  // current date window, so we compute them from RAW filtered only by the
  // non-date filters that match the current drill-down slice.
  const adsetMeta = PAYLOAD.adsets || {};
  const today = PAYLOAD.until;
  const dayMs = 86400000;
  const d3 = new Date(today + 'T00:00:00'); d3.setDate(d3.getDate() - 2);   // 3D inclusive of today = today-2..today
  const d7 = new Date(today + 'T00:00:00'); d7.setDate(d7.getDate() - 6);   // 7D inclusive
  const d3Str = `${d3.getFullYear()}-${String(d3.getMonth()+1).padStart(2,'0')}-${String(d3.getDate()).padStart(2,'0')}`;
  const d7Str = `${d7.getFullYear()}-${String(d7.getMonth()+1).padStart(2,'0')}-${String(d7.getDate()).padStart(2,'0')}`;

  // Pre-aggregate rolling totals per adset_id from RAW (all dates),
  // applying the same non-date filters that produced the current rows[].
  const rolling = new Map();
  const passNonDate = (r) => {
    if (F.portals.size && !F.portals.has(r.portal)) return false;
    if (F.categories.size && !F.categories.has(r.category)) return false;
    if (F.creative_types.size && !F.creative_types.has(r.creative_type)) return false;
    if (F.product_families.size && !F.product_families.has(productFamily(r.product))) return false;
    return true;
  };
  for (const r of RAW) {
    if (!r.adset_id) continue;
    if (!passNonDate(r)) continue;
    if (!rolling.has(r.adset_id)) rolling.set(r.adset_id, {
      today_s:0, today_r:0, d3_s:0, d3_r:0, d7_s:0, d7_r:0
    });
    const x = rolling.get(r.adset_id);
    if (r.date === today)  { x.today_s += r.spend||0; x.today_r += r.revenue||0; }
    if (r.date >= d3Str)   { x.d3_s    += r.spend||0; x.d3_r    += r.revenue||0; }
    if (r.date >= d7Str)   { x.d7_s    += r.spend||0; x.d7_r    += r.revenue||0; }
  }
  const safeRoas = (rev, sp) => sp > 0 ? rev/sp : 0;

  const adsetRows = [...byAdset.entries()].map(([id, v]) => {
    const meta = adsetMeta[id] || {};
    const roll = rolling.get(id) || { today_s:0, today_r:0, d3_s:0, d3_r:0, d7_s:0, d7_r:0 };
    return {
      adset_id: id,
      campaign: v.campaign_name,
      adset: v.adset_name,
      portal: v.portal,
      incl: meta.audiences_incl || '',
      excl: meta.audiences_excl || '',
      ads: v.ads.size,
      spend: v.spend,
      revenue: v.revenue,
      orders: v.purchases,
      roas:       v.spend > 0 ? v.revenue / v.spend : 0,
      roas_today: safeRoas(roll.today_r, roll.today_s),
      roas_3d:    safeRoas(roll.d3_r,    roll.d3_s),
      roas_7d:    safeRoas(roll.d7_r,    roll.d7_s),
    };
  }).sort((a, b) => b.spend - a.spend).slice(0, 50);

  const audienceCell = (s) => {
    if (!s) return '<span class="subtle">—</span>';
    const escaped = s.replace(/"/g, '&quot;');
    return `<span class="cell-name" style="max-width:240px;display:inline-block;vertical-align:middle" title="${escaped}">${s}</span>`;
  };

  const roasOrDash = (n) => (n == null || !isFinite(n) || n <= 0) ? '<span class="subtle">—</span>' : fmt.roas(n);

  document.getElementById('drill-adset-tbody').innerHTML = adsetRows.map(r =>
    `<tr>` +
      `<td>` +
        `<span class="cell-name" style="max-width:340px;display:inline-block;vertical-align:middle" title="${(r.campaign+' / '+r.adset).replace(/"/g, '&quot;')}">` +
          `<strong>${r.campaign}</strong><br>` +
          `<span class="subtle">↳ ${r.adset}</span>` +
        `</span>` +
      `</td>` +
      `<td>${r.portal}</td>` +
      `<td>${audienceCell(r.incl)}</td>` +
      `<td>${audienceCell(r.excl)}</td>` +
      `<td>${fmt.num(r.ads)}</td>` +
      `<td>${fmt.inr(r.spend)}</td>` +
      `<td>${fmt.num(r.orders)}</td>` +
      `<td>${fmt.roas(r.roas)}</td>` +
      `<td>${roasOrDash(r.roas_today)}</td>` +
      `<td>${roasOrDash(r.roas_3d)}</td>` +
      `<td>${roasOrDash(r.roas_7d)}</td>` +
    `</tr>`
  ).join('') || '<tr><td colspan="11" class="empty">No data for current filter.</td></tr>';
}

function renderCategoriesPage(rows) {
  const cats = aggregate(rows, 'category').filter(c => c.key);
  for (const c of cats) {
    const subset = rows.filter(r => r.category === c.key);
    c.success_rate = successRate(subset);
    c.category = c.key;
  }
  const onPage = document.getElementById('page-categories').classList.contains('active');
  if (onPage) {
    // Pie chart: BUDGET allocation by category (not spend). Slice size is
    // the sum of ACTIVE campaign daily-budgets whose ads are dominantly
    // in that category. Tooltip also surfaces actual spend + ROAS so the
    // operator can spot "budget allocated but not spent" or "spending
    // more than allocated".
    const campMeta = PAYLOAD.campaigns || {};
    // For each campaign that has spend in the window, decide its dominant
    // category (the category bucket with the most ad-day spend inside the
    // campaign). Use UNFILTERED rows so a campaign that mixes categories
    // still gets a single dominant bucket.
    const campCat = new Map();   // campaign_id -> dominant category
    const campCatSpend = new Map(); // campaign_id -> { cat: spend, ... }
    for (const r of RAW) {
      if (!r.campaign_id || !r.category) continue;
      if (!campCatSpend.has(r.campaign_id)) campCatSpend.set(r.campaign_id, {});
      const m = campCatSpend.get(r.campaign_id);
      m[r.category] = (m[r.category] || 0) + (r.spend || 0);
    }
    for (const [cid, m] of campCatSpend) {
      let bestCat = null, bestSpend = -1;
      for (const [k, v] of Object.entries(m)) if (v > bestSpend) { bestCat = k; bestSpend = v; }
      campCat.set(cid, bestCat);
    }
    // Now sum daily_budget per category (ACTIVE only).
    const budgetByCat = new Map();
    for (const [cid, meta] of Object.entries(campMeta)) {
      if (meta.status !== 'ACTIVE') continue;
      const cat = campCat.get(cid);
      if (!cat) continue;
      const b = (meta.daily_budget || 0);
      if (b <= 0) continue;
      budgetByCat.set(cat, (budgetByCat.get(cat) || 0) + b);
    }
    // Build pie data sorted by budget desc. Cross-reference cats for spend/ROAS.
    const spendByCat = new Map();
    cats.forEach(c => spendByCat.set(c.key, c));
    const pieRows = [...budgetByCat.entries()]
      .map(([cat, budget]) => {
        const c = spendByCat.get(cat) || { spend: 0, roas: 0 };
        return { cat, budget, spend: c.spend, roas: c.roas };
      })
      .filter(r => r.budget > 0)
      .sort((a, b) => b.budget - a.budget);
    const totalBudget = pieRows.reduce((s, r) => s + r.budget, 0);
    const pieEmpty = pieRows.length === 0;
    document.getElementById('chart-cat-pie').style.display = pieEmpty ? 'none' : '';
    document.getElementById('cat-pie-empty').style.display = pieEmpty ? 'block' : 'none';
    if (!pieEmpty) {
      pieChart('chart-cat-pie',
        pieRows.map(r => r.cat),
        [{
          label: 'Daily budget',
          data: pieRows.map(r => Math.round(r.budget)),
          backgroundColor: pieRows.map(r => catColor(r.cat)),
          borderColor: '#fff',
          borderWidth: 2,
        }],
        {
          plugins: {
            legend: { position: 'right', labels: { boxWidth: 14, font: { size: 12 } } },
            tooltip: {
              callbacks: {
                label: (ctx) => {
                  const r = pieRows[ctx.dataIndex];
                  const pct = totalBudget > 0 ? (r.budget / totalBudget * 100).toFixed(1) : '0';
                  const spendPct = r.budget > 0 ? (r.spend / r.budget * 100).toFixed(0) : '–';
                  return [
                    `${r.cat}`,
                    `Daily budget: ₹${Math.round(r.budget).toLocaleString('en-IN')} (${pct}% of total)`,
                    `Spent (window): ₹${Math.round(r.spend).toLocaleString('en-IN')} · ROAS ${r.roas.toFixed(2)}x`,
                    `Utilization: ${spendPct}% of allocated daily × days`,
                  ];
                }
              }
            }
          }
        });
    }

    // Bar chart: spend + ROAS aggregated over the window (existing view)
    barChart('chart-cat-bar',
      cats.map(c => c.category),
      [
        { label:'Spend (₹)', data: cats.map(c => Math.round(c.spend)), backgroundColor:cats.map(c => catColor(c.category)), yAxisID:'y' },
        { label:'ROAS',      data: cats.map(c => +c.roas.toFixed(2)),  backgroundColor:'#059669', type:'line', yAxisID:'y1', tension:.25 },
      ],
      { type:'bar', scales:{
        y:{ beginAtZero:true, position:'left', ticks:{ callback:v => '₹' + (v >= 100000 ? (v/100000).toFixed(1)+'L' : v) } },
        y1:{ beginAtZero:true, position:'right', grid:{ drawOnChartArea:false } },
      } });

    // Daily trend — one line per category over the selected window.
    // Single-day windows have no lines to draw; show a hint and hide the
    // empty canvas so the area doesn't look broken.
    const cts = categoryTimeSeries(rows);
    const singleDay = cts.dates.length <= 1;
    const toggle = (canvasId, hintId, hide) => {
      const c = document.getElementById(canvasId);
      const h = document.getElementById(hintId);
      if (c) c.style.display = hide ? 'none' : '';
      if (h) h.style.display = hide ? 'block' : 'none';
    };
    toggle('chart-cat-trend-spend', 'cat-trend-spend-hint', singleDay);
    toggle('chart-cat-trend-roas',  'cat-trend-roas-hint',  singleDay);
    if (!singleDay) {
      const tsForLineChart = cts.dates.map(d => ({ date: d }));
      lineChart('chart-cat-trend-spend', tsForLineChart, cts.spend, {
        plugins:{ legend:{ position:'bottom' } },
        scales:{ y:{ beginAtZero:true, ticks:{ callback:v => '₹' + (v >= 100000 ? (v/100000).toFixed(1)+'L' : (v >= 1000 ? (v/1000).toFixed(0)+'K' : v)) } } },
        interaction:{ mode:'index', intersect:false },
      });
      lineChart('chart-cat-trend-roas', tsForLineChart, cts.roas, {
        plugins:{ legend:{ position:'bottom' } },
        scales:{ y:{ beginAtZero:true, ticks:{ callback:v => v.toFixed(1) + 'x' } } },
        interaction:{ mode:'index', intersect:false },
      });
    }

    // Drill-down: visible only when operator picks a Product or a single Category.
    // Otherwise we'd be summing across all categories and the per-creative /
    // per-campaign tables get unmanageable.
    renderDrillDown(rows);
  }
  const sorted = sortRows(cats, 'tbl-categories');
  applySortHeaders('tbl-categories');
  document.querySelector('#tbl-categories tbody').innerHTML = sorted.map(c =>
    `<tr><td><strong>${c.category}</strong></td>` +
    `<td>${fmt.num(c.active_ads)}</td><td>${fmt.num(c.active_camps)}</td>` +
    `<td>${fmt.inr(c.spend)}</td><td>${fmt.inr(c.revenue)}</td><td>${fmt.num(c.purchases)}</td>` +
    `<td>${fmt.roas(c.roas)}</td><td>${fmt.inr(c.cpm)}</td><td>${fmt.num1(c.ctr)}</td>` +
    `<td>${fmt.num1(c.atc_rate)}</td>` +
    `<td>${c.success_rate == null ? '<span class="subtle">—</span>' : fmt.pct(c.success_rate)}</td></tr>`
  ).join('') || '<tr><td colspan="11" class="empty">No data.</td></tr>';
}

function renderCreativesPage(rows) {
  const cts = aggregate(rows, 'creative_type').filter(c => c.key);
  for (const c of cts) {
    const subset = rows.filter(r => r.creative_type === c.key);
    c.success_rate = successRate(subset);
    c.creative_type = c.key;
  }
  if (document.getElementById('page-creatives').classList.contains('active')) {
    barChart('chart-ct-bar',
      cts.map(c => c.creative_type),
      [
        { label:'Spend (₹)', data: cts.map(c => Math.round(c.spend)), backgroundColor:'#1a3d7c', yAxisID:'y' },
        { label:'ROAS',      data: cts.map(c => +c.roas.toFixed(2)),  backgroundColor:'#059669', type:'line', yAxisID:'y1', tension:.25 },
      ],
      { type:'bar', scales:{
        y:{ beginAtZero:true, position:'left', ticks:{ callback:v => '₹' + (v >= 100000 ? (v/100000).toFixed(1)+'L' : v) } },
        y1:{ beginAtZero:true, position:'right', grid:{ drawOnChartArea:false } },
      } });
  }
  const sorted = sortRows(cts, 'tbl-creatives');
  applySortHeaders('tbl-creatives');
  document.querySelector('#tbl-creatives tbody').innerHTML = sorted.map(c =>
    `<tr><td><strong>${c.creative_type}</strong></td>` +
    `<td>${fmt.num(c.active_ads)}</td><td>${fmt.inr(c.spend)}</td><td>${fmt.inr(c.revenue)}</td>` +
    `<td>${fmt.num(c.purchases)}</td><td>${fmt.roas(c.roas)}</td>` +
    `<td>${c.success_rate == null ? '<span class="subtle">—</span>' : fmt.pct(c.success_rate)}</td></tr>`
  ).join('') || '<tr><td colspan="7" class="empty">No data.</td></tr>';
}

function renderSentimentsPage(rows) {
  const map = new Map();
  for (const r of rows) {
    const key = (r.sentiment || '(unset)') + '||' + (r.creative_type || '?');
    if (!map.has(key)) map.set(key, {
      sentiment: r.sentiment || '(unset)',
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
    `<tr><td><strong>${s.sentiment}</strong></td><td>${s.creative_type}</td>` +
    `<td>${fmt.num(s.active_ads)}</td><td>${fmt.inr(s.spend)}</td>` +
    `<td>${fmt.inr(s.revenue)}</td><td>${fmt.roas(s.roas)}</td></tr>`
  ).join('') || '<tr><td colspan="6" class="empty">No data.</td></tr>';
}

function renderHeatmapPage(rows) {
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

// Product Budget page: roll up daily_budget per product (via dominant
// product label per campaign) and per campaign. Shows current day's
// utilization + rolling ROAS so the operator can see where the budget
// is allocated and whether each bucket is earning its keep.
function renderProdBudgetPage(rows) {
  const campMeta = PAYLOAD.campaigns || {};

  // 1) Determine each campaign's dominant product label from RAW
  //    (full payload — so utilization calcs aren't biased by the
  //    operator's current date filter).
  const campSpendByProd = new Map();   // campaign_id -> { prod -> spend }
  for (const r of RAW) {
    if (!r.campaign_id) continue;
    const p = r.product || '(no product tag)';
    if (!campSpendByProd.has(r.campaign_id)) campSpendByProd.set(r.campaign_id, {});
    const m = campSpendByProd.get(r.campaign_id);
    m[p] = (m[p] || 0) + (r.spend || 0);
  }
  const campToProduct = new Map();
  for (const [cid, m] of campSpendByProd) {
    let bestProd = '(no product tag)', bestSp = -1;
    for (const [k, v] of Object.entries(m)) if (v > bestSp) { bestProd = k; bestSp = v; }
    campToProduct.set(cid, bestProd);
  }

  // 2) Compute per-campaign rolling spend + revenue from RAW
  const today = PAYLOAD.until;
  const d3 = new Date(today + 'T00:00:00'); d3.setDate(d3.getDate() - 2);
  const d7 = new Date(today + 'T00:00:00'); d7.setDate(d7.getDate() - 6);
  const fmtDate = d => `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;
  const d3Str = fmtDate(d3), d7Str = fmtDate(d7);

  const campStats = new Map();   // campaign_id -> { spend_today, rev_today, spend_3d, rev_3d, spend_7d, rev_7d, spend_window, rev_window }
  const inWindow = (r) => r.date >= F.fromDate && r.date <= F.toDate;
  for (const r of RAW) {
    if (!r.campaign_id) continue;
    if (!campStats.has(r.campaign_id)) campStats.set(r.campaign_id, {
      spend_today:0, rev_today:0, spend_3d:0, rev_3d:0,
      spend_7d:0, rev_7d:0, spend_window:0, rev_window:0,
    });
    const x = campStats.get(r.campaign_id);
    const sp = r.spend || 0, rv = r.revenue || 0;
    if (r.date === today)  { x.spend_today += sp; x.rev_today += rv; }
    if (r.date >= d3Str)   { x.spend_3d    += sp; x.rev_3d    += rv; }
    if (r.date >= d7Str)   { x.spend_7d    += sp; x.rev_7d    += rv; }
    if (inWindow(r))       { x.spend_window+= sp; x.rev_window+= rv; }
  }
  const safeRoas = (rev, sp) => sp > 0 ? rev/sp : 0;

  // 3) Build per-campaign rows (ACTIVE with daily_budget > 0 only)
  const campRows = [];
  for (const [cid, meta] of Object.entries(campMeta)) {
    if (meta.status !== 'ACTIVE') continue;
    const budget = meta.daily_budget || 0;
    if (budget <= 0) continue;
    const stats = campStats.get(cid) || {
      spend_today:0, rev_today:0, spend_3d:0, rev_3d:0,
      spend_7d:0, rev_7d:0, spend_window:0, rev_window:0,
    };
    campRows.push({
      campaign_id: cid,
      name: meta.name || '(unnamed)',
      product: campToProduct.get(cid) || '(no product tag)',
      portal: meta.portal || '',
      daily_budget: budget,
      spend_today: stats.spend_today,
      utilization: budget > 0 ? (stats.spend_today / budget * 100) : 0,
      spend_7d: stats.spend_7d,
      roas_today: safeRoas(stats.rev_today, stats.spend_today),
      roas_3d:    safeRoas(stats.rev_3d,    stats.spend_3d),
      roas_7d:    safeRoas(stats.rev_7d,    stats.spend_7d),
    });
  }

  // 4) KPI strip
  const totalBudget = campRows.reduce((s, r) => s + r.daily_budget, 0);
  const totalSpendToday = campRows.reduce((s, r) => s + r.spend_today, 0);
  const overallUtil = totalBudget > 0 ? (totalSpendToday / totalBudget * 100) : 0;
  const totalRev7d = campRows.reduce((s, r) => s + r.spend_7d * r.roas_7d, 0);
  const totalSp7d  = campRows.reduce((s, r) => s + r.spend_7d, 0);
  const blended7dRoas = totalSp7d > 0 ? totalRev7d / totalSp7d : 0;

  const kpis = [
    { l:'Active Campaigns',    v: fmt.num(campRows.length), s:'with daily budget configured' },
    { l:'Total Daily Budget',  v: fmt.inr(totalBudget),     s:'sum across active campaigns' },
    { l:'Spend Today',         v: fmt.inr(totalSpendToday), s:`${overallUtil.toFixed(0)}% of allocated` },
    { l:'7D Spend',            v: fmt.inr(totalSp7d),       s:'last 7 days' },
    { l:'7D ROAS (blended)',   v: fmt.roas(blended7dRoas),  s:'Meta-pixel-attributed', cls: blended7dRoas >= 2 ? 'good' : (blended7dRoas >= 1.5 ? 'warn' : 'bad') },
  ];
  document.getElementById('kpi-strip-prodbudget').innerHTML = kpis.map(c =>
    `<div class="kpi-card${c.cls ? ' kpi-' + c.cls : ''}"><div class="kpi-lbl">${c.l}</div>` +
    `<div class="kpi-val">${c.v}</div><div class="kpi-sub">${c.s}</div></div>`
  ).join('');

  // 5) Per-product rollup
  const prodMap = new Map();
  for (const r of campRows) {
    if (!prodMap.has(r.product)) prodMap.set(r.product, {
      product: r.product, camps:0, daily_budget:0, spend_today:0, spend_7d:0,
      rev_7d_acc: 0, spend_window:0, rev_window:0,
    });
    const x = prodMap.get(r.product);
    x.camps += 1;
    x.daily_budget += r.daily_budget;
    x.spend_today  += r.spend_today;
    x.spend_7d     += r.spend_7d;
    x.rev_7d_acc   += (r.spend_7d * r.roas_7d);     // recover revenue from roas*spend
    const s = campStats.get(r.campaign_id) || { spend_window:0, rev_window:0 };
    x.spend_window += s.spend_window;
    x.rev_window   += s.rev_window;
  }
  const prodRows = [...prodMap.values()].map(x => ({
    ...x,
    utilization: x.daily_budget > 0 ? (x.spend_today / x.daily_budget * 100) : 0,
    roas_7d:     x.spend_7d > 0     ? x.rev_7d_acc / x.spend_7d   : 0,
    roas_window: x.spend_window > 0 ? x.rev_window / x.spend_window : 0,
  }));

  // 6) Render — both tables sortable. Default sort: daily budget DESC.
  const sortedProds = sortRows(prodRows, 'tbl-prodbudget');
  applySortHeaders('tbl-prodbudget');
  document.querySelector('#tbl-prodbudget tbody').innerHTML = sortedProds.map(r =>
    `<tr><td><strong>${r.product}</strong></td>` +
    `<td>${fmt.num(r.camps)}</td>` +
    `<td>${fmt.inr(r.daily_budget)}</td>` +
    `<td>${fmt.inr(r.spend_today)}</td>` +
    `<td>${r.utilization.toFixed(0)}%</td>` +
    `<td>${fmt.inr(r.spend_7d)}</td>` +
    `<td>${fmt.roas(r.roas_7d)}</td>` +
    `<td>${fmt.roas(r.roas_window)}</td></tr>`
  ).join('') || '<tr><td colspan="8" class="empty">No active campaigns with daily budget.</td></tr>';

  const sortedCamps = sortRows(campRows, 'tbl-campbudget').slice(0, 100);
  applySortHeaders('tbl-campbudget');
  document.querySelector('#tbl-campbudget tbody').innerHTML = sortedCamps.map(r =>
    `<tr><td><span class="cell-name" style="max-width:340px;display:inline-block;vertical-align:middle" title="${(r.name||'').replace(/"/g, '&quot;')}">${r.name}</span></td>` +
    `<td>${r.product}</td>` +
    `<td>${r.portal}</td>` +
    `<td>${fmt.inr(r.daily_budget)}</td>` +
    `<td>${fmt.inr(r.spend_today)}</td>` +
    `<td>${r.utilization.toFixed(0)}%</td>` +
    `<td>${fmt.inr(r.spend_7d)}</td>` +
    `<td>${r.roas_today > 0 ? fmt.roas(r.roas_today) : '<span class="subtle">—</span>'}</td>` +
    `<td>${r.roas_3d    > 0 ? fmt.roas(r.roas_3d)    : '<span class="subtle">—</span>'}</td>` +
    `<td>${r.roas_7d    > 0 ? fmt.roas(r.roas_7d)    : '<span class="subtle">—</span>'}</td></tr>`
  ).join('') || '<tr><td colspan="10" class="empty">No active campaigns with daily budget.</td></tr>';
}

function renderProductsPage(rows) {
  const prods = aggregate(rows, 'product').filter(x => x.key);
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
    return `<span class="${cls}">${v.toFixed(0)}%</span><br><span class="subtle">${r[key + '_h']}/${r[key + '_n']}</span>`;
  }
  document.querySelector('#tbl-products tbody').innerHTML = sorted.map(p =>
    `<tr>
      <td><strong class="cell-name" title="${p.product}">${p.product}</strong></td>
      <td>${p.category || '<span class="subtle">—</span>'}</td>
      <td>${fmt.num(p.active_ads)}</td><td>${fmt.inr(p.spend)}</td>
      <td>${fmt.inr(p.revenue)}</td><td>${fmt.num(p.orders)}</td>
      <td>${fmt.roas(p.roas)}</td>
      <td>${rateCell(p, 1.5)}</td><td>${rateCell(p, 2.0)}</td><td>${rateCell(p, 2.5)}</td>
      <td>${rateCell(p, 3.0)}</td><td>${rateCell(p, 4.0)}</td><td>${rateCell(p, 5.0)}</td>
    </tr>`
  ).join('') || '<tr><td colspan="13" class="empty">No data.</td></tr>';
}

function renderProdSuccessPage(rows) {
  // Campaign-level success: for each product, group ad-days into campaigns,
  // compute campaign ROAS (over the selected period), then bucket campaigns
  // by ROAS thresholds.
  const ROASBKT = [1.5, 2.0, 2.5, 3.0, 4.0, 5.0];
  const MIN_CAMP_SPEND = 500;
  // product -> Map<campaign_id, {spend, revenue, purchases, name, category}>
  const byProd = new Map();
  for (const r of rows) {
    if (!r.product) continue;
    if (!byProd.has(r.product)) byProd.set(r.product, new Map());
    const camps = byProd.get(r.product);
    if (!camps.has(r.campaign_id)) camps.set(r.campaign_id, {
      spend:0, revenue:0, purchases:0,
      campaign_name:r.campaign_name, category:r.category,
    });
    const c = camps.get(r.campaign_id);
    c.spend     += r.spend     || 0;
    c.revenue   += r.revenue   || 0;
    c.purchases += r.purchases || 0;
  }
  const out = [];
  for (const [product, camps] of byProd) {
    const list = [...camps.values()].filter(c => c.spend >= MIN_CAMP_SPEND);
    if (list.length === 0) continue;
    let spend = 0, revenue = 0, orders = 0, bestRoas = 0;
    for (const c of list) {
      spend += c.spend; revenue += c.revenue; orders += c.purchases;
      const cr = c.spend > 0 ? c.revenue / c.spend : 0;
      if (cr > bestRoas) bestRoas = cr;
    }
    const row = {
      product, category: list[0].category || '',
      campaigns: list.length,
      spend, revenue, orders,
      roas: spend > 0 ? revenue / spend : 0,
      best_roas: bestRoas,
    };
    ROASBKT.forEach(thr => {
      const key = 'hit_' + String(thr).replace('.', '').padEnd(2, '0').slice(0, 2);
      const hit = list.filter(c => (c.spend > 0 ? c.revenue / c.spend : 0) >= thr).length;
      row[key] = list.length > 0 ? Math.round(100 * hit / list.length * 10) / 10 : null;
      row[key + '_n'] = list.length;
      row[key + '_h'] = hit;
    });
    out.push(row);
  }
  const sorted = sortRows(out, 'tbl-prodsuccess');
  applySortHeaders('tbl-prodsuccess');
  function rateCell(r, thr) {
    const key = 'hit_' + String(thr).replace('.', '').padEnd(2, '0').slice(0, 2);
    const v = r[key];
    if (v == null) return '<span class="subtle">—</span>';
    const cls = v >= 50 ? 'sr-hi' : v >= 25 ? 'sr-med' : v >= 10 ? 'sr-low' : 'sr-0';
    return `<span class="${cls}">${v.toFixed(0)}%</span><br><span class="subtle">${r[key + '_h']}/${r[key + '_n']}</span>`;
  }
  document.querySelector('#tbl-prodsuccess tbody').innerHTML = sorted.map(p =>
    `<tr>
      <td><strong class="cell-name" title="${p.product}">${p.product}</strong></td>
      <td>${p.category || '<span class="subtle">—</span>'}</td>
      <td><strong>${fmt.num(p.campaigns)}</strong></td>
      <td>${fmt.inr(p.spend)}</td>
      <td>${fmt.inr(p.revenue)}</td>
      <td>${fmt.num(p.orders)}</td>
      <td>${fmt.roas(p.roas)}</td>
      <td>${fmt.roas(p.best_roas)}</td>
      <td>${rateCell(p, 1.5)}</td><td>${rateCell(p, 2.0)}</td><td>${rateCell(p, 2.5)}</td>
      <td>${rateCell(p, 3.0)}</td><td>${rateCell(p, 4.0)}</td><td>${rateCell(p, 5.0)}</td>
    </tr>`
  ).join('') || '<tr><td colspan="14" class="empty">No products with campaigns ≥ ₹500 spend in selection.</td></tr>';
}

function renderAdsPage(rows, which) {
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
  ads.sort((a, b) => which === 'top' ? b.roas - a.roas : a.roas - b.roas);
  const top50 = ads.slice(0, 50);
  const tableId = which === 'top' ? 'tbl-topads' : 'tbl-bottomads';
  const sorted = sortRows(top50, tableId);
  applySortHeaders(tableId);
  document.querySelector(`#${tableId} tbody`).innerHTML = sorted.map(a =>
    `<tr>
      <td>${tag(a.portal)}</td>
      <td>${a.category || '<span class="subtle">—</span>'}</td>
      <td>${a.creative_type || '<span class="subtle">—</span>'}</td>
      <td><div class="cell-name" title="${(a.ad_name||'').replace(/"/g,'&quot;')}"><strong>${a.ad_name||'?'}</strong><br><span class="subtle">${a.campaign_name||''}</span></div></td>
      <td>${a.days_active != null ? a.days_active + 'd' : '—'}</td>
      <td>${fmt.inr(a.spend)}</td><td>${fmt.inr(a.revenue)}</td>
      <td>${fmt.num(a.orders)}</td><td>${fmt.roas(a.roas)}</td>
    </tr>`
  ).join('') || `<tr><td colspan="9" class="empty">No ads with ≥ ₹2K spend in selection.</td></tr>`;
}

// ── CSV export ──────────────────────────────────────────────────────────
function exportCSV(which) {
  const rows = applyFilters(RAW);
  let csv = '';
  if (which === 'products') {
    const prods = aggregate(rows, 'product').filter(x => x.key);
    csv = 'Product,Active Ads,Spend,Revenue,Orders,ROAS\n' +
      prods.map(p => `"${p.key}",${p.active_ads},${Math.round(p.spend)},${Math.round(p.revenue)},${p.purchases},${p.roas.toFixed(2)}`).join('\n');
  } else if (which === 'prodsuccess') {
    const ROASBKT = [1.5, 2.0, 2.5, 3.0, 4.0, 5.0];
    const MIN = 500;
    const byProd = new Map();
    for (const r of rows) {
      if (!r.product) continue;
      if (!byProd.has(r.product)) byProd.set(r.product, new Map());
      const m = byProd.get(r.product);
      if (!m.has(r.campaign_id)) m.set(r.campaign_id, { spend:0, revenue:0, purchases:0, category:r.category });
      const c = m.get(r.campaign_id);
      c.spend += r.spend||0; c.revenue += r.revenue||0; c.purchases += r.purchases||0;
    }
    csv = 'Product,Category,CampaignsPublished,Spend,Revenue,Orders,AggROAS,BestCampROAS,' +
          ROASBKT.map(t => `Hit${t}x_pct,Hit${t}x_count,Hit${t}x_total`).join(',') + '\n';
    for (const [product, camps] of byProd) {
      const list = [...camps.values()].filter(c => c.spend >= MIN);
      if (!list.length) continue;
      let s=0,r=0,o=0,b=0;
      for (const c of list) { s+=c.spend; r+=c.revenue; o+=c.purchases; const cr=c.spend>0?c.revenue/c.spend:0; if (cr>b)b=cr; }
      const buckets = ROASBKT.map(t => {
        const h = list.filter(c => (c.spend>0?c.revenue/c.spend:0) >= t).length;
        return `${list.length>0?(100*h/list.length).toFixed(1):0},${h},${list.length}`;
      }).join(',');
      csv += `"${product}","${list[0].category||''}",${list.length},${Math.round(s)},${Math.round(r)},${o},${(s>0?r/s:0).toFixed(2)},${b.toFixed(2)},${buckets}\n`;
    }
  } else {
    csv = 'Portal,Category,Creative,Ad,Campaign,Spend,Revenue,Orders,ROAS,DaysActive\n';
    const map = new Map();
    for (const r of rows) {
      if (!map.has(r.ad_id)) map.set(r.ad_id, { ...r, _spend:0, _revenue:0, _orders:0 });
      const a = map.get(r.ad_id);
      a._spend += r.spend; a._revenue += r.revenue; a._orders += r.purchases || 0;
    }
    let ads = [...map.values()].filter(a => a._spend >= 1000);
    if (which === 'topads') ads.sort((a, b) => (b._revenue / Math.max(b._spend, 1)) - (a._revenue / Math.max(a._spend, 1)));
    if (which === 'bottomads') ads.sort((a, b) => (a._revenue / Math.max(a._spend, 1)) - (b._revenue / Math.max(b._spend, 1)));
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

// ── Master apply (re-renders every page-relevant section) ───────────────
function apply() {
  const rows = applyFilters(RAW);
  const prevRows = getCompareSet();
  // Always render Overview KPI strip (visible on Overview page)
  renderOverview(rows, prevRows);
  // Render whichever page is active for charts/tables that need a visible canvas
  const activePage = document.querySelector('.page.active');
  const id = activePage ? activePage.id.replace('page-', '') : 'overview';
  if (id === 'trends')      renderTrends(rows);
  if (id === 'categories')  renderCategoriesPage(rows);
  if (id === 'creatives')   renderCreativesPage(rows);
  if (id === 'sentiments')  renderSentimentsPage(rows);
  if (id === 'heatmap')     renderHeatmapPage(rows);
  if (id === 'prodbudget')  renderProdBudgetPage(rows);
  if (id === 'products')    renderProductsPage(rows);
  if (id === 'prodsuccess') renderProdSuccessPage(rows);
  if (id === 'topads')      renderAdsPage(rows, 'top');
  if (id === 'bottomads')   renderAdsPage(rows, 'bottom');
  // Always pre-compute non-chart tables for unsorted state — cheap
  // (charts skip themselves when their canvas is hidden)
}

// ── Setup sorts + initial render ────────────────────────────────────────
setupSort('tbl-categories', 'spend');
setupSort('tbl-creatives',  'spend');
setupSort('tbl-prodbudget', 'daily_budget');
setupSort('tbl-campbudget', 'daily_budget');
setupSort('tbl-products',   'spend');
setupSort('tbl-prodsuccess','spend');
setupSort('tbl-topads',     'roas', 'desc');
setupSort('tbl-bottomads',  'roas', 'asc');
setupSort('tbl-sentiment',  'spend');

document.querySelector('.preset-btn[data-days="1"]').click();
activatePageFromHash();
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
    shopify_daily = fetch_shopify_daily(conn, since, until)
    adsets = fetch_adsets(conn, since, until)
    campaigns = fetch_active_campaign_budgets(conn, since, until)

    payload = {
        'since': since,
        'until': until,
        'rows': rows,
        'shopify_daily': shopify_daily,    # Real orders + revenue per portal/date
        'adsets': adsets,                   # adset_id -> {name, audiences_incl, audiences_excl, ...}
        'campaigns': campaigns,             # campaign_id -> {name, daily_budget, status, portal}
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
