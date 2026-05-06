#!/usr/bin/env python3
"""
NTN Dashboard v2 — render categories_v2.html from SQLite.

Reads ONLY from state/ntn.db. No Meta/Shopify API calls. The dashboard
will never break on rate limits because it doesn't talk to those APIs.

Sections rendered:
  1. Top KPI strip (selected period)
  2. Category cards (Skin, Hair, Crystal HD, Crystal Acc, Aibot, Nutra,
     24K Jewellery, Perfumes, Other) with active ads / spend / revenue /
     ROAS / CPM / CTR / ATC rate
  3. Per-category breakdown by creative type (Paras/Motion/Static/etc.)
     with success rate (% of ads that survived 7+ days)
  4. Per-creative-type sentiment breakdown
  5. Top 10 + Bottom 10 ads (by ROAS) with CID + campaign name
  6. Product drill-down (sidebar) with success rates at multiple ROAS
     thresholds (>=1.5x, >=2x, >=2.5x, >=3x, >=4x, >=5x)

Data is embedded as JSON. JS-side date filter swaps which slice renders.

Usage:
  python3 scripts/v2/build_dashboard.py                    # default last 30 days
  python3 scripts/v2/build_dashboard.py --days 7
  python3 scripts/v2/build_dashboard.py --since 2026-04-25 --until 2026-05-05
  python3 scripts/v2/build_dashboard.py --out custom.html
"""

import argparse
import json
import sys
from datetime import datetime, date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _utils import db_connect, IST, now_iso  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
OUT_DIR = REPO_ROOT / 'out'
OUT_DIR.mkdir(parents=True, exist_ok=True)

CATEGORIES = ['Skin', 'Hair', 'Crystal Home Decor', 'Crystal Accessory',
              '24K Jewellery', 'Perfumes', 'Aibot', 'Nutraceuticals', 'Other']
CREATIVE_TYPES = ['Paras', 'Wanda', 'Partnership', 'AI', 'Motion', 'Static', 'Other']
PORTALS = ['SM', 'SML', 'NBP']

# ROAS thresholds for product drill-down "success at X" metric
ROAS_BUCKETS = [1.5, 2.0, 2.5, 3.0, 4.0, 5.0]


# ── Data fetchers ─────────────────────────────────────────────────────────
def fetch_overall_kpis(conn, since, until):
    """Top strip: total spend, revenue, orders, blended ROAS, active ads,
    active campaigns. Across all portals."""
    row = conn.execute('''
        SELECT
          COUNT(DISTINCT d.ad_id)                   AS n_ads,
          COUNT(DISTINCT d.campaign_id)             AS n_camps,
          COALESCE(SUM(d.spend), 0)                 AS spend,
          COALESCE(SUM(d.revenue), 0)               AS revenue,
          COALESCE(SUM(d.purchases), 0)             AS purchases,
          COALESCE(SUM(d.impressions), 0)           AS impressions,
          COALESCE(SUM(d.clicks), 0)                AS clicks
        FROM meta_ads_daily d
        WHERE d.date BETWEEN ? AND ? AND d.spend > 0
    ''', (since, until)).fetchone()
    spend = row[2]; revenue = row[3]
    return {
        'active_ads':     row[0],
        'active_camps':   row[1],
        'spend':          round(spend, 2),
        'revenue':        round(revenue, 2),
        'purchases':      row[4],
        'impressions':    row[5],
        'clicks':         row[6],
        'roas':           round(revenue / spend, 2) if spend > 0 else 0,
        'cpm':            round((spend / row[5]) * 1000, 2) if row[5] > 0 else 0,
        'ctr':            round((row[6] / row[5]) * 100, 2) if row[5] > 0 else 0,
    }


def fetch_per_portal_kpis(conn, since, until):
    """Same metrics, split by portal."""
    out = {}
    for p in PORTALS:
        row = conn.execute('''
            SELECT
              COUNT(DISTINCT d.ad_id),
              COUNT(DISTINCT d.campaign_id),
              COALESCE(SUM(d.spend), 0),
              COALESCE(SUM(d.revenue), 0),
              COALESCE(SUM(d.purchases), 0),
              COALESCE(SUM(d.impressions), 0),
              COALESCE(SUM(d.clicks), 0)
            FROM meta_ads_daily d
            WHERE d.date BETWEEN ? AND ? AND d.portal = ? AND d.spend > 0
        ''', (since, until, p)).fetchone()
        spend = row[2]; revenue = row[3]
        out[p] = {
            'active_ads': row[0], 'active_camps': row[1],
            'spend': round(spend, 2), 'revenue': round(revenue, 2),
            'purchases': row[4], 'impressions': row[5], 'clicks': row[6],
            'roas': round(revenue / spend, 2) if spend > 0 else 0,
            'cpm': round((spend / row[5]) * 1000, 2) if row[5] > 0 else 0,
            'ctr': round((row[6] / row[5]) * 100, 2) if row[5] > 0 else 0,
        }
    return out


def fetch_category_summary(conn, since, until):
    """Per-category aggregate metrics for the period."""
    out = {}
    for cat in CATEGORIES:
        row = conn.execute('''
            SELECT
              COUNT(DISTINCT d.ad_id),
              COUNT(DISTINCT d.campaign_id),
              COALESCE(SUM(d.spend), 0),
              COALESCE(SUM(d.revenue), 0),
              COALESCE(SUM(d.purchases), 0),
              COALESCE(SUM(d.impressions), 0),
              COALESCE(SUM(d.clicks), 0),
              COALESCE(SUM(d.add_to_cart), 0)
            FROM meta_ads_daily d
            JOIN meta_ads_meta  m ON d.ad_id = m.ad_id
            WHERE d.date BETWEEN ? AND ? AND m.category = ? AND d.spend > 0
        ''', (since, until, cat)).fetchone()
        spend = row[2]; revenue = row[3]
        out[cat] = {
            'active_ads': row[0], 'active_camps': row[1],
            'spend': round(spend, 2), 'revenue': round(revenue, 2),
            'purchases': row[4], 'impressions': row[5],
            'clicks': row[6], 'atc': row[7],
            'roas': round(revenue / spend, 2) if spend > 0 else 0,
            'cpm': round((spend / row[5]) * 1000, 2) if row[5] > 0 else 0,
            'ctr': round((row[6] / row[5]) * 100, 2) if row[5] > 0 else 0,
            'atc_rate': round((row[7] / row[6]) * 100, 2) if row[6] > 0 else 0,
        }
    return out


def fetch_creative_type_breakdown(conn, since, until, *, per_category: bool = True):
    """Per (category, creative_type) aggregates with success rate.
    Success rate = % of ads with first_seen in window where days_active >= 7.
    """
    out = {}
    if per_category:
        keys = [(cat, ct) for cat in CATEGORIES for ct in CREATIVE_TYPES]
    else:
        keys = [(None, ct) for ct in CREATIVE_TYPES]

    for cat, ct in keys:
        where = ['d.date BETWEEN ? AND ?', 'm.creative_type = ?', 'd.spend > 0']
        args  = [since, until, ct]
        if cat is not None:
            where.append('m.category = ?')
            args.append(cat)
        where_sql = ' AND '.join(where)

        # Aggregates (spend, ROAS, etc.)
        row = conn.execute(f'''
            SELECT
              COUNT(DISTINCT d.ad_id),
              COALESCE(SUM(d.spend), 0),
              COALESCE(SUM(d.revenue), 0),
              COALESCE(SUM(d.purchases), 0)
            FROM meta_ads_daily d
            JOIN meta_ads_meta  m ON d.ad_id = m.ad_id
            WHERE {where_sql}
        ''', args).fetchone()
        if row[0] == 0:
            continue   # nothing to surface
        spend = row[1]; revenue = row[2]

        # Success rate: ads first_seen within [since, until], days_active >= 7
        # (ads must be old enough to have had a chance to survive 7d)
        sr_where = ['m.creative_type = ?', 'm.first_seen IS NOT NULL',
                    'm.first_seen BETWEEN ? AND ?']
        sr_args  = [ct, since, until]
        if cat is not None:
            sr_where.append('m.category = ?')
            sr_args.append(cat)
        sr_sql_where = ' AND '.join(sr_where)
        sr = conn.execute(f'''
            SELECT
              COUNT(*) AS launched,
              SUM(CASE WHEN days_active >= 7 THEN 1 ELSE 0 END) AS survived
            FROM meta_ads_meta m
            WHERE {sr_sql_where}
        ''', sr_args).fetchone()
        launched, survived = (sr[0] or 0), (sr[1] or 0)

        bucket = {
            'active_ads': row[0],
            'spend': round(spend, 2),
            'revenue': round(revenue, 2),
            'purchases': row[3],
            'roas': round(revenue / spend, 2) if spend > 0 else 0,
            'launched': launched,
            'survived_7d': survived,
            'success_rate': round(100 * survived / launched, 1) if launched > 0 else None,
        }
        out_key = f'{cat}|{ct}' if cat is not None else ct
        out[out_key] = bucket
    return out


def fetch_sentiment_breakdown(conn, since, until):
    """(creative_type, sentiment) aggregates. Sentiment may be NULL → '(unset)'."""
    out = {}
    for r in conn.execute('''
        SELECT
          m.creative_type,
          COALESCE(m.sentiment, '(unset)') AS sentiment,
          COALESCE(sl.label, m.sentiment) AS sentiment_label,
          COUNT(DISTINCT d.ad_id) AS n_ads,
          COALESCE(SUM(d.spend), 0)    AS spend,
          COALESCE(SUM(d.revenue), 0)  AS revenue,
          COALESCE(SUM(d.purchases), 0) AS purchases
        FROM meta_ads_daily d
        JOIN meta_ads_meta  m ON d.ad_id = m.ad_id
        LEFT JOIN sentiment_labels sl ON sl.code = m.sentiment
        WHERE d.date BETWEEN ? AND ? AND d.spend > 0
        GROUP BY m.creative_type, m.sentiment
        ORDER BY spend DESC
    ''', (since, until)).fetchall():
        ct, sent, label, n_ads, spend, revenue, purchases = r
        key = f'{ct}|{sent}'
        out[key] = {
            'creative_type': ct,
            'sentiment_code': sent,
            'sentiment_label': label or sent,
            'active_ads': n_ads,
            'spend': round(spend, 2),
            'revenue': round(revenue, 2),
            'purchases': purchases,
            'roas': round(revenue / spend, 2) if spend > 0 else 0,
        }
    return out


def fetch_top_ads(conn, since, until, *, limit=10, order='best'):
    """Top/bottom N ads by lifetime ROAS in the window. Includes campaign
    name for context. min_spend filter avoids surfacing ₹50-spent flukes."""
    direction = 'DESC' if order == 'best' else 'ASC'
    rows = conn.execute(f'''
        SELECT
          d.ad_id,
          d.ad_name,
          d.campaign_id,
          d.campaign_name,
          d.portal,
          m.category,
          m.creative_type,
          SUM(d.spend)        AS spend,
          SUM(d.revenue)      AS revenue,
          SUM(d.purchases)    AS purchases,
          MAX(m.days_active)  AS days_active
        FROM meta_ads_daily d
        LEFT JOIN meta_ads_meta m ON d.ad_id = m.ad_id
        WHERE d.date BETWEEN ? AND ? AND d.spend > 0
        GROUP BY d.ad_id
        HAVING SUM(d.spend) >= 2000
        ORDER BY (SUM(d.revenue) / NULLIF(SUM(d.spend), 0)) {direction}
        LIMIT ?
    ''', (since, until, limit)).fetchall()
    out = []
    for r in rows:
        spend = r[7] or 0; revenue = r[8] or 0
        out.append({
            'ad_id': r[0],
            'ad_name': r[1],
            'campaign_id': r[2],
            'campaign_name': r[3],
            'portal': r[4],
            'category': r[5],
            'creative_type': r[6],
            'spend': round(spend, 2),
            'revenue': round(revenue, 2),
            'purchases': r[9],
            'roas': round(revenue / spend, 2) if spend > 0 else 0,
            'days_active': r[10],
        })
    return out


def fetch_product_drill(conn, since, until):
    """Per-product aggregates with success-rate at multiple ROAS thresholds.
    Joins via NTN code → product_ntn_labels. Ads without NTN/product show
    under '(no product tag)'."""
    out = {}
    rows = conn.execute('''
        SELECT
          COALESCE(m.product, p.product, m.ntn_code, '(no product tag)') AS prod_name,
          m.category,
          COUNT(DISTINCT d.ad_id) AS n_ads,
          COUNT(DISTINCT d.campaign_id) AS n_camps,
          COALESCE(SUM(d.spend), 0)    AS spend,
          COALESCE(SUM(d.revenue), 0)  AS revenue,
          COALESCE(SUM(d.purchases), 0) AS purchases,
          COALESCE(SUM(d.impressions), 0) AS impressions
        FROM meta_ads_daily d
        JOIN meta_ads_meta  m ON d.ad_id = m.ad_id
        LEFT JOIN product_ntn_labels p ON p.ntn_code = m.ntn_code
        WHERE d.date BETWEEN ? AND ? AND d.spend > 0
        GROUP BY prod_name, m.category
        ORDER BY spend DESC
    ''', (since, until)).fetchall()

    for product, category, n_ads, n_camps, spend, revenue, purchases, impressions in rows:
        # Per-product success rate at each ROAS threshold:
        # of ads launched in window, what % crossed each ROAS threshold over their lifetime?
        success = {}
        for thr in ROAS_BUCKETS:
            row = conn.execute('''
                SELECT COUNT(*),
                       SUM(CASE WHEN total_spend > 0
                                AND total_revenue / total_spend >= ?
                                THEN 1 ELSE 0 END)
                FROM meta_ads_meta
                WHERE category = ? AND product = ?
                  AND first_seen BETWEEN ? AND ?
            ''', (thr, category, product, since, until)).fetchone()
            launched, hit = (row[0] or 0), (row[1] or 0)
            success[f'{thr}x'] = {
                'launched': launched,
                'hit': hit,
                'rate': round(100 * hit / launched, 1) if launched > 0 else None,
            }

        key = f'{product}|{category or "Other"}'
        out[key] = {
            'product': product,
            'category': category or 'Other',
            'active_ads': n_ads,
            'active_camps': n_camps,
            'spend': round(spend, 2),
            'revenue': round(revenue, 2),
            'purchases': purchases,
            'impressions': impressions,
            'roas': round(revenue / spend, 2) if spend > 0 else 0,
            'success_at_roas': success,
        }
    return out


def fetch_freshness(conn):
    """When did each ingest job last run?"""
    out = {}
    for job_name, target_date, status, started_at, finished_at, rows_written in conn.execute('''
        SELECT job_name, target_date, status, started_at, finished_at, rows_written
        FROM ingest_log
        WHERE (job_name, started_at) IN (
          SELECT job_name, MAX(started_at) FROM ingest_log GROUP BY job_name
        )
    ''').fetchall():
        out[job_name] = {
            'target_date': target_date,
            'status': status,
            'started_at': started_at,
            'finished_at': finished_at,
            'rows_written': rows_written,
        }
    return out


# ── HTML render ───────────────────────────────────────────────────────────
def render_html(data: dict, since: str, until: str, period_label: str) -> str:
    payload = json.dumps(data, default=str, ensure_ascii=False)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>📊 NTN Categories v2 — {period_label}</title>
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{ font-family: -apple-system,'Segoe UI',Roboto,sans-serif; background:#f0f4fb; color:#1a1a2e; }}
.header {{ background:linear-gradient(135deg,#0d2145,#1a3d7c); padding:18px 24px; color:#fff; }}
.header h1 {{ font-size:18px; margin-bottom:3px; }}
.header p {{ font-size:11px; color:rgba(255,255,255,.7); }}
.last-upd {{ font-size:10px; color:rgba(255,255,255,.5); margin-top:4px; }}
.tabbar {{ background:#fff; padding:10px 24px; border-bottom:1px solid #dde3f0; display:flex; gap:14px; flex-wrap:wrap; }}
.tab {{ padding:6px 12px; border-radius:8px; cursor:pointer; font-size:12px; font-weight:700; color:#374151; border:1px solid #e5e7eb; background:#fff; }}
.tab.active {{ background:#1a3d7c; color:#fff; border-color:#1a3d7c; }}
.main {{ padding:18px 24px; max-width:1500px; margin:0 auto; }}
.kpi-strip {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr)); gap:12px; margin-bottom:20px; }}
.kpi {{ background:#fff; border-radius:12px; padding:14px 16px; border:1px solid #dde3f0; }}
.kpi-lbl {{ font-size:10px; color:#6b7280; text-transform:uppercase; letter-spacing:.6px; font-weight:700; }}
.kpi-val {{ font-size:20px; font-weight:800; color:#0d2145; margin-top:3px; }}
.kpi-sub {{ font-size:10px; color:#6b7280; margin-top:2px; }}
.section {{ background:#fff; border-radius:12px; padding:16px 18px; border:1px solid #dde3f0; margin-bottom:18px; }}
.section h2 {{ font-size:14px; font-weight:800; color:#0d2145; padding-bottom:8px; border-bottom:2px solid #eef2ff; margin-bottom:12px; }}
.section h3 {{ font-size:12px; font-weight:700; color:#374151; margin-top:14px; margin-bottom:8px; }}
table {{ width:100%; border-collapse:collapse; font-size:12px; }}
th {{ background:#f8faff; color:#374151; padding:8px 10px; text-align:left; font-size:10px; text-transform:uppercase; letter-spacing:.4px; border-bottom:1px solid #dde3f0; }}
th:not(:first-child) {{ text-align:right; }}
td {{ padding:8px 10px; border-bottom:1px solid #f3f4f6; }}
td:not(:first-child) {{ text-align:right; font-variant-numeric:tabular-nums; }}
.cat-grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(280px,1fr)); gap:14px; margin-bottom:18px; }}
.cat-card {{ background:#fff; border-radius:12px; padding:14px 16px; border:1px solid #dde3f0; cursor:pointer; transition:all .15s; }}
.cat-card:hover {{ transform:translateY(-2px); box-shadow:0 4px 12px rgba(0,0,0,.08); }}
.cat-card.selected {{ border-color:#1a3d7c; box-shadow:0 0 0 2px #1a3d7c33; }}
.cat-name {{ font-size:14px; font-weight:800; color:#0d2145; margin-bottom:6px; }}
.cat-mini {{ display:grid; grid-template-columns:repeat(2,1fr); gap:6px; font-size:11px; }}
.cat-mini-row {{ display:flex; justify-content:space-between; }}
.cat-mini-lbl {{ color:#6b7280; }}
.cat-mini-val {{ font-weight:700; color:#0d2145; }}
.rg {{ color:#059669; font-weight:700; }}
.ro {{ color:#d97706; font-weight:700; }}
.rr {{ color:#dc2626; font-weight:700; }}
.tag {{ display:inline-block; padding:2px 6px; border-radius:4px; font-size:10px; font-weight:700; }}
.tag-sm  {{ background:#dbeafe; color:#1d4ed8; }}
.tag-sml {{ background:#d1fae5; color:#065f46; }}
.tag-nbp {{ background:#fef3c7; color:#92400e; }}
.empty {{ color:#9ca3af; font-style:italic; padding:8px; text-align:center; }}
.subtle {{ color:#6b7280; font-size:10px; }}
.ad-name {{ max-width:380px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }}
</style>
</head>
<body>
<div class="header">
  <h1>📊 NTN Categories v2</h1>
  <p>SQLite-backed · Reads from state/ntn.db · Period: {since} → {until} ({period_label})</p>
  <p class="last-upd">Built {data['updated_at']} · Reads from {data['db_path']}</p>
</div>

<div class="tabbar">
  <span class="subtle" style="margin-right:8px">📂 Category drill:</span>
  <button class="tab active" data-cat="all">All</button>
  {''.join(f'<button class="tab" data-cat="{c}">{c}</button>' for c in CATEGORIES)}
</div>

<div class="main">

  <!-- Top KPI strip -->
  <div class="kpi-strip" id="kpi-strip"></div>

  <!-- Per-portal split -->
  <div class="section">
    <h2>📡 Per-Portal Split</h2>
    <table>
      <thead><tr>
        <th>Portal</th><th>Active Ads</th><th>Active Camps</th>
        <th>Spend (₹)</th><th>Revenue (₹)</th><th>Orders</th>
        <th>ROAS</th><th>CPM (₹)</th><th>CTR (%)</th>
      </tr></thead>
      <tbody id="portal-rows"></tbody>
    </table>
  </div>

  <!-- Category cards -->
  <div class="section">
    <h2>📂 Categories — click to drill</h2>
    <div class="cat-grid" id="cat-cards"></div>
  </div>

  <!-- Drill-down section: creative type breakdown for selected category -->
  <div class="section">
    <h2>🎨 Creative Type Breakdown <span class="subtle" id="drill-title">(All Categories)</span></h2>
    <table>
      <thead><tr>
        <th>Creative Type</th><th>Active Ads</th><th>Spend (₹)</th>
        <th>Revenue (₹)</th><th>ROAS</th>
        <th>Launched in Period</th><th>Survived 7d</th><th>Success Rate</th>
      </tr></thead>
      <tbody id="creative-rows"></tbody>
    </table>
  </div>

  <!-- Sentiment breakdown -->
  <div class="section">
    <h2>💬 Sentiment Breakdown <span class="subtle">(per creative type)</span></h2>
    <table>
      <thead><tr>
        <th>Creative Type</th><th>Sentiment</th><th>Code</th>
        <th>Active Ads</th><th>Spend (₹)</th><th>Revenue (₹)</th><th>ROAS</th>
      </tr></thead>
      <tbody id="sentiment-rows"></tbody>
    </table>
    <p class="subtle" style="margin-top:8px">
      Sentiment codes (st1, st2…) come from ad name nomenclature.
      Update <code>sentiment_labels</code> table to set human-readable labels.
    </p>
  </div>

  <!-- Top + Bottom ads -->
  <div class="section">
    <h2>🏆 Top 10 by ROAS</h2>
    <table>
      <thead><tr>
        <th>Portal</th><th>Cat</th><th>Type</th><th>Ad / Campaign</th>
        <th>Days Active</th><th>Spend</th><th>Revenue</th><th>ROAS</th>
      </tr></thead>
      <tbody id="top-rows"></tbody>
    </table>
  </div>
  <div class="section">
    <h2>🥶 Bottom 10 by ROAS <span class="subtle">(min ₹2K spend, kill candidates)</span></h2>
    <table>
      <thead><tr>
        <th>Portal</th><th>Cat</th><th>Type</th><th>Ad / Campaign</th>
        <th>Days Active</th><th>Spend</th><th>Revenue</th><th>ROAS</th>
      </tr></thead>
      <tbody id="bottom-rows"></tbody>
    </table>
  </div>

  <!-- Product drill-down (sidebar concept rendered as section here) -->
  <div class="section">
    <h2>🛍️ Product Drill-Down — Success Rate at ROAS Thresholds</h2>
    <table>
      <thead><tr>
        <th>Product</th><th>Category</th>
        <th>Active Ads</th><th>Spend</th><th>Revenue</th><th>ROAS</th>
        <th>≥1.5x</th><th>≥2.0x</th><th>≥2.5x</th><th>≥3.0x</th><th>≥4.0x</th><th>≥5.0x</th>
      </tr></thead>
      <tbody id="product-rows"></tbody>
    </table>
    <p class="subtle" style="margin-top:8px">
      "≥X.Xx" = % of ads launched in this period whose lifetime ROAS hit that threshold.
    </p>
  </div>

</div>

<script>
const DATA = {payload};
const fmt = {{
  inr: (n) => n == null ? '—' : '₹' + Math.round(n).toLocaleString('en-IN'),
  num: (n) => n == null ? '—' : Math.round(n).toLocaleString('en-IN'),
  pct: (n) => n == null ? '—' : n.toFixed(1) + '%',
  roas: (n) => {{
    if (n == null) return '—';
    const cls = n >= 2.5 ? 'rg' : (n >= 1.5 ? 'ro' : 'rr');
    return `<span class="${{cls}}">${{n.toFixed(2)}}x</span>`;
  }},
}};
function tag(p) {{ return `<span class="tag tag-${{p.toLowerCase()}}">${{p}}</span>`; }}

// Render top KPI strip
function renderKpi() {{
  const k = DATA.overall;
  const el = document.getElementById('kpi-strip');
  el.innerHTML = [
    {{l:'Active Ads', v: fmt.num(k.active_ads), s: k.active_camps + ' campaigns'}},
    {{l:'Spend',      v: fmt.inr(k.spend), s: 'across all portals'}},
    {{l:'Revenue',    v: fmt.inr(k.revenue), s: k.purchases + ' purchases'}},
    {{l:'Blended ROAS', v: fmt.roas(k.roas), s: 'spend-weighted'}},
    {{l:'CPM',        v: fmt.inr(k.cpm), s: 'cost per 1k impr'}},
    {{l:'CTR',        v: fmt.pct(k.ctr), s: 'click-through'}},
  ].map(c =>
    `<div class="kpi"><div class="kpi-lbl">${{c.l}}</div>` +
    `<div class="kpi-val">${{c.v}}</div><div class="kpi-sub">${{c.s}}</div></div>`
  ).join('');
}}

function renderPortals() {{
  const tbody = document.getElementById('portal-rows');
  const portals = ['SM','SML','NBP'];
  tbody.innerHTML = portals.map(p => {{
    const r = DATA.per_portal[p];
    return `<tr><td>${{tag(p)}}</td>` +
      `<td>${{fmt.num(r.active_ads)}}</td><td>${{fmt.num(r.active_camps)}}</td>` +
      `<td>${{fmt.inr(r.spend)}}</td><td>${{fmt.inr(r.revenue)}}</td>` +
      `<td>${{fmt.num(r.purchases)}}</td>` +
      `<td>${{fmt.roas(r.roas)}}</td>` +
      `<td>${{fmt.inr(r.cpm)}}</td><td>${{fmt.pct(r.ctr)}}</td></tr>`;
  }}).join('');
}}

function renderCategories() {{
  const grid = document.getElementById('cat-cards');
  const order = Object.entries(DATA.categories).sort((a,b) => b[1].spend - a[1].spend);
  grid.innerHTML = order.map(([cat, r]) => {{
    const sel = window.SELECTED_CAT === cat ? ' selected' : '';
    return `<div class="cat-card${{sel}}" data-cat="${{cat}}">
      <div class="cat-name">${{cat}}</div>
      <div class="cat-mini">
        <div class="cat-mini-row"><span class="cat-mini-lbl">Active Ads</span><span class="cat-mini-val">${{fmt.num(r.active_ads)}}</span></div>
        <div class="cat-mini-row"><span class="cat-mini-lbl">Camps</span><span class="cat-mini-val">${{fmt.num(r.active_camps)}}</span></div>
        <div class="cat-mini-row"><span class="cat-mini-lbl">Spend</span><span class="cat-mini-val">${{fmt.inr(r.spend)}}</span></div>
        <div class="cat-mini-row"><span class="cat-mini-lbl">Revenue</span><span class="cat-mini-val">${{fmt.inr(r.revenue)}}</span></div>
        <div class="cat-mini-row"><span class="cat-mini-lbl">ROAS</span><span class="cat-mini-val">${{fmt.roas(r.roas)}}</span></div>
        <div class="cat-mini-row"><span class="cat-mini-lbl">CPM</span><span class="cat-mini-val">${{fmt.inr(r.cpm)}}</span></div>
        <div class="cat-mini-row"><span class="cat-mini-lbl">CTR</span><span class="cat-mini-val">${{fmt.pct(r.ctr)}}</span></div>
        <div class="cat-mini-row"><span class="cat-mini-lbl">ATC Rate</span><span class="cat-mini-val">${{fmt.pct(r.atc_rate)}}</span></div>
      </div>
    </div>`;
  }}).join('');
}}

function renderCreativeBreakdown(selectedCat) {{
  document.getElementById('drill-title').textContent = selectedCat === 'all'
    ? '(All Categories — Total)'
    : `(${{selectedCat}})`;

  // Build matching keys
  const tbody = document.getElementById('creative-rows');
  let entries;
  if (selectedCat === 'all') {{
    // Aggregate across categories per creative_type
    const agg = {{}};
    for (const [k,v] of Object.entries(DATA.creative_breakdown)) {{
      const ct = k.split('|')[1];
      if (!agg[ct]) agg[ct] = {{ active_ads:0, spend:0, revenue:0, purchases:0, launched:0, survived_7d:0 }};
      agg[ct].active_ads   += v.active_ads;
      agg[ct].spend        += v.spend;
      agg[ct].revenue      += v.revenue;
      agg[ct].purchases    += v.purchases;
      agg[ct].launched     += v.launched;
      agg[ct].survived_7d  += v.survived_7d;
    }}
    entries = Object.entries(agg).map(([ct, a]) => [ct, {{
      ...a,
      roas: a.spend > 0 ? a.revenue/a.spend : 0,
      success_rate: a.launched > 0 ? 100 * a.survived_7d / a.launched : null,
    }}]);
  }} else {{
    entries = Object.entries(DATA.creative_breakdown)
      .filter(([k]) => k.split('|')[0] === selectedCat)
      .map(([k,v]) => [k.split('|')[1], v]);
  }}

  entries.sort((a,b) => b[1].spend - a[1].spend);
  if (entries.length === 0) {{
    tbody.innerHTML = '<tr><td colspan="8" class="empty">No data for this category in selected period.</td></tr>';
    return;
  }}
  tbody.innerHTML = entries.map(([ct, r]) => `
    <tr>
      <td><strong>${{ct}}</strong></td>
      <td>${{fmt.num(r.active_ads)}}</td>
      <td>${{fmt.inr(r.spend)}}</td>
      <td>${{fmt.inr(r.revenue)}}</td>
      <td>${{fmt.roas(r.roas)}}</td>
      <td>${{fmt.num(r.launched)}}</td>
      <td>${{fmt.num(r.survived_7d)}}</td>
      <td>${{r.success_rate == null ? '<span class="subtle">—</span>' : fmt.pct(r.success_rate)}}</td>
    </tr>
  `).join('');
}}

function renderSentiment() {{
  const tbody = document.getElementById('sentiment-rows');
  const entries = Object.entries(DATA.sentiment).sort((a,b) => b[1].spend - a[1].spend);
  if (entries.length === 0) {{
    tbody.innerHTML = '<tr><td colspan="7" class="empty">No sentiment data yet — start tagging ads with _st1_, _st2_, etc.</td></tr>';
    return;
  }}
  tbody.innerHTML = entries.map(([k, r]) => `
    <tr>
      <td><strong>${{r.creative_type}}</strong></td>
      <td>${{r.sentiment_label || r.sentiment_code}}</td>
      <td><code class="subtle">${{r.sentiment_code}}</code></td>
      <td>${{fmt.num(r.active_ads)}}</td>
      <td>${{fmt.inr(r.spend)}}</td>
      <td>${{fmt.inr(r.revenue)}}</td>
      <td>${{fmt.roas(r.roas)}}</td>
    </tr>
  `).join('');
}}

function renderTopBottom() {{
  function row(r) {{
    const camp = r.campaign_name || '';
    return `<tr>
      <td>${{tag(r.portal || '?')}}</td>
      <td>${{r.category || '?'}}</td>
      <td>${{r.creative_type || '?'}}</td>
      <td><div class="ad-name" title="${{(r.ad_name||'').replace(/"/g,'&quot;')}}"><strong>${{r.ad_name || '?'}}</strong><br/><span class="subtle">${{camp}}</span></div></td>
      <td>${{fmt.num(r.days_active)}}d</td>
      <td>${{fmt.inr(r.spend)}}</td>
      <td>${{fmt.inr(r.revenue)}}</td>
      <td>${{fmt.roas(r.roas)}}</td>
    </tr>`;
  }}
  document.getElementById('top-rows').innerHTML = DATA.top_ads.map(row).join('');
  document.getElementById('bottom-rows').innerHTML = DATA.bottom_ads.map(row).join('');
}}

function renderProducts() {{
  const tbody = document.getElementById('product-rows');
  const entries = Object.entries(DATA.products).sort((a,b) => b[1].spend - a[1].spend);
  tbody.innerHTML = entries.map(([k, r]) => {{
    const sr = r.success_at_roas;
    function rateCell(thr) {{
      const b = sr[thr];
      if (!b || b.launched === 0) return '<span class="subtle">—</span>';
      return `${{fmt.pct(b.rate)}} <span class="subtle">(${{b.hit}}/${{b.launched}})</span>`;
    }}
    return `<tr>
      <td><strong>${{r.product}}</strong></td>
      <td>${{r.category}}</td>
      <td>${{fmt.num(r.active_ads)}}</td>
      <td>${{fmt.inr(r.spend)}}</td>
      <td>${{fmt.inr(r.revenue)}}</td>
      <td>${{fmt.roas(r.roas)}}</td>
      <td>${{rateCell('1.5x')}}</td>
      <td>${{rateCell('2.0x')}}</td>
      <td>${{rateCell('2.5x')}}</td>
      <td>${{rateCell('3.0x')}}</td>
      <td>${{rateCell('4.0x')}}</td>
      <td>${{rateCell('5.0x')}}</td>
    </tr>`;
  }}).join('');
}}

// Tab interaction
window.SELECTED_CAT = 'all';
function bindTabs() {{
  document.querySelectorAll('.tab').forEach(t => {{
    t.addEventListener('click', () => {{
      window.SELECTED_CAT = t.dataset.cat;
      document.querySelectorAll('.tab').forEach(x => x.classList.remove('active'));
      t.classList.add('active');
      renderCreativeBreakdown(window.SELECTED_CAT);
      renderCategories();  // re-render to update selection state
    }});
  }});
  document.getElementById('cat-cards').addEventListener('click', (e) => {{
    const card = e.target.closest('.cat-card');
    if (!card) return;
    const cat = card.dataset.cat;
    window.SELECTED_CAT = cat;
    document.querySelectorAll('.tab').forEach(x => {{
      x.classList.toggle('active', x.dataset.cat === cat);
    }});
    renderCreativeBreakdown(cat);
    renderCategories();
  }});
}}

// Bootstrap
renderKpi();
renderPortals();
renderCategories();
renderCreativeBreakdown('all');
renderSentiment();
renderTopBottom();
renderProducts();
bindTabs();
</script>
</body>
</html>"""


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--days', type=int, default=30,
                   help='Default 30. Period = today minus N days.')
    p.add_argument('--since', help='YYYY-MM-DD (overrides --days)')
    p.add_argument('--until', help='YYYY-MM-DD (overrides --days)')
    p.add_argument('--out', default=str(OUT_DIR / 'categories_v2.html'),
                   help='Output HTML path')
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
    period_label = f"last {(end - start).days + 1} days"

    conn = db_connect(Path(args.db)) if args.db else db_connect()

    print(f"📊 Building dashboard")
    print(f"   Period: {since} → {until} ({period_label})")

    data = {
        'period': period_label,
        'since': since,
        'until': until,
        'overall':            fetch_overall_kpis(conn, since, until),
        'per_portal':         fetch_per_portal_kpis(conn, since, until),
        'categories':         fetch_category_summary(conn, since, until),
        'creative_breakdown': fetch_creative_type_breakdown(conn, since, until),
        'sentiment':          fetch_sentiment_breakdown(conn, since, until),
        'top_ads':            fetch_top_ads(conn, since, until, limit=10, order='best'),
        'bottom_ads':         fetch_top_ads(conn, since, until, limit=10, order='worst'),
        'products':           fetch_product_drill(conn, since, until),
        'freshness':          fetch_freshness(conn),
        'updated_at':         now_iso(),
        'db_path':            str(args.db or 'state/ntn.db'),
    }

    html = render_html(data, since, until, period_label)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding='utf-8')
    print(f"\n✅ Wrote {out_path}  ({len(html):,} bytes)")
    conn.close()


if __name__ == '__main__':
    main()
