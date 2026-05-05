#!/usr/bin/env python3
"""
Shopify UTM Orders Report — pulls orders from SM/SML/NBP stores, parses UTMs
from landing_site (and falls back to note_attributes), and aggregates by
campaign × product so we can see which Meta campaign drove which Shopify
products.

Outputs two tabs to the GHA reports sheet:
  📦 UTM Orders Summary YYYY-MM-DD : one row per (portal, utm_campaign)
  📦 UTM Orders Detail YYYY-MM-DD  : one row per (portal, campaign, product)

Usage:
  python3 scripts/shopify_utm_orders.py                 # last 7 days, all portals
  python3 scripts/shopify_utm_orders.py --days 30       # last 30 days
  python3 scripts/shopify_utm_orders.py --days 7 --portal SM
  python3 scripts/shopify_utm_orders.py --since 2026-05-01 --until 2026-05-04
"""

import os, sys, time, argparse, requests
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse, parse_qs
from collections import defaultdict
from pathlib import Path
from zoneinfo import ZoneInfo

import gspread
from google.oauth2.service_account import Credentials
from dotenv import load_dotenv

# ── Path + env setup ──────────────────────────────────────────────────────
_REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_REPO_ROOT / '.env')

SA_FILE  = os.environ.get('GOOGLE_SERVICE_ACCOUNT_FILE') or str(_REPO_ROOT / 'google-service-account.json')
SHEET_ID = os.environ.get('REPORTS_SHEET_ID') or '1hJ3IS2VDtTAEyyJIV__jvts9CMQdYhyxKAfWKtrkUH4'
SCOPES   = ['https://www.googleapis.com/auth/spreadsheets']
IST      = ZoneInfo('Asia/Kolkata')

PORTAL_SHOPIFY = {
    'SM':  (os.getenv('SHOPIFY_STORE_URL'),     os.getenv('SHOPIFY_ACCESS_TOKEN')),
    'SML': (os.getenv('SHOPIFY_STORE_URL_SML'), os.getenv('SHOPIFY_ACCESS_TOKEN_SML')),
    'NBP': (os.getenv('SHOPIFY_STORE_URL_NBP'), os.getenv('SHOPIFY_ACCESS_TOKEN_NBP')),
}

API_VERSION = '2024-01'

# Shopify REST: 2 req/s burst, ~40/min sustained. Sleep between paged calls.
PAGE_SLEEP_S = 0.6


def safe_float(v):
    try: return float(str(v).replace(',', '').strip())
    except: return 0.0


def parse_utm_from_landing_site(url):
    """Extract utm_* params from a landing_site URL. Returns dict or empty {}."""
    if not url:
        return {}
    try:
        qs = parse_qs(urlparse(url).query)
        out = {}
        for k in ('utm_source', 'utm_medium', 'utm_campaign', 'utm_content', 'utm_term'):
            v = qs.get(k, [''])[0].strip()
            if v: out[k] = v
        return out
    except Exception:
        return {}


def parse_utm_from_note_attrs(note_attrs):
    """Some merchants store UTMs in note_attributes (key/value pairs).
    Returns dict of any utm_* keys found.
    """
    out = {}
    if not note_attrs: return out
    for attr in note_attrs:
        name = (attr.get('name') or '').strip().lower()
        if name.startswith('utm_'):
            v = (attr.get('value') or '').strip()
            if v: out[name] = v
    return out


def extract_utm(order):
    """Best-effort UTM extraction. landing_site wins, note_attrs fills gaps."""
    utm = parse_utm_from_landing_site(order.get('landing_site') or '')
    note_utm = parse_utm_from_note_attrs(order.get('note_attributes') or [])
    for k, v in note_utm.items():
        utm.setdefault(k, v)
    # Also check referring_site as last resort for source
    if 'utm_source' not in utm:
        ref = (order.get('referring_site') or '').lower()
        if 'facebook' in ref or 'fb.com' in ref:
            utm['utm_source'] = 'facebook'
        elif 'instagram' in ref:
            utm['utm_source'] = 'instagram'
    return utm


def fetch_orders(portal, since_iso, until_iso):
    """Paginated Shopify orders fetch. Returns list of order dicts."""
    url, token = PORTAL_SHOPIFY[portal]
    if not (url and token):
        print(f"  ⚠️  Shopify creds missing for {portal} — skipping")
        return []

    api = f'https://{url}/admin/api/{API_VERSION}/orders.json'
    params = {
        'created_at_min': since_iso,
        'created_at_max': until_iso,
        'limit': 250,
        'status': 'any',
        # Pull only the fields we need to keep payload small
        'fields': ','.join([
            'id', 'name', 'created_at', 'total_price', 'subtotal_price',
            'currency', 'cancelled_at', 'financial_status',
            'landing_site', 'referring_site', 'note_attributes',
            'source_name', 'source_identifier',
            'line_items', 'customer',
        ]),
    }
    headers = {'X-Shopify-Access-Token': token}
    out = []
    page = 0
    next_url = api
    next_params = params
    try:
        while next_url:
            page += 1
            r = requests.get(next_url, params=next_params, headers=headers, timeout=60)
            if r.status_code == 429:
                wait_s = int(r.headers.get('Retry-After', '5'))
                print(f"  ⏳ {portal} rate-limited, sleep {wait_s}s")
                time.sleep(wait_s)
                continue
            if r.status_code != 200:
                print(f"  ⚠️  {portal} HTTP {r.status_code} on page {page}: {r.text[:200]}")
                break
            orders = r.json().get('orders', [])
            out.extend(orders)
            print(f"  {portal} page {page}: {len(orders)} orders (running total {len(out)})")

            # Cursor pagination via Link header
            link = r.headers.get('Link', '')
            next_url = None
            next_params = None  # subsequent calls use the full Link URL
            for part in link.split(','):
                if 'rel="next"' in part:
                    seg = part.strip().split(';')[0].strip().strip('<>')
                    next_url = seg
                    break
            if next_url:
                time.sleep(PAGE_SLEEP_S)
    except Exception as e:
        print(f"  ⚠️  {portal} fetch error: {e}")
    return out


def aggregate(orders, portal):
    """Returns (campaign_summary, campaign_product_detail) dicts.

    campaign_summary[(portal, campaign)] = {
        'utm_source': ..., 'utm_medium': ..., 'orders': 0, 'revenue': 0.0,
        'product_units': defaultdict(int), 'product_revenue': defaultdict(float),
        'first_seen': ..., 'last_seen': ...,
    }
    """
    out = defaultdict(lambda: {
        'utm_source': '', 'utm_medium': '',
        'orders': 0, 'revenue': 0.0,
        'product_units': defaultdict(int),
        'product_revenue': defaultdict(float),
        'first_seen': None, 'last_seen': None,
    })

    for o in orders:
        if o.get('cancelled_at'):
            continue
        utm = extract_utm(o)
        campaign = utm.get('utm_campaign', '(no_utm_campaign)')
        source   = utm.get('utm_source', '(direct)')
        medium   = utm.get('utm_medium', '(none)')
        key = (portal, campaign)
        bucket = out[key]
        if not bucket['utm_source']: bucket['utm_source'] = source
        if not bucket['utm_medium']: bucket['utm_medium'] = medium
        bucket['orders'] += 1
        bucket['revenue'] += safe_float(o.get('total_price'))

        # Track first/last order timestamps
        ca = o.get('created_at', '')
        if ca:
            if bucket['first_seen'] is None or ca < bucket['first_seen']:
                bucket['first_seen'] = ca
            if bucket['last_seen'] is None or ca > bucket['last_seen']:
                bucket['last_seen'] = ca

        # Sum products
        for li in o.get('line_items', []) or []:
            title = (li.get('title') or 'Unknown').strip()
            qty = int(li.get('quantity') or 0)
            line_rev = safe_float(li.get('price')) * qty
            bucket['product_units'][title] += qty
            bucket['product_revenue'][title] += line_rev
    return out


def build_summary_rows(agg):
    """One row per (portal, campaign): orders, revenue, top product, distinct products."""
    headers = [
        'Portal', 'UTM Campaign', 'UTM Source', 'UTM Medium',
        'Orders', 'Revenue (₹)', 'AOV (₹)',
        'Top Product (units)', '# Distinct Products', 'All Products (top 5 by units)',
        'First Order', 'Last Order',
    ]
    rows = [headers]
    # Sort by revenue desc within portal
    items = sorted(agg.items(), key=lambda kv: (kv[0][0], -kv[1]['revenue']))
    for (portal, campaign), b in items:
        prods_sorted = sorted(b['product_units'].items(), key=lambda x: -x[1])
        top = prods_sorted[0] if prods_sorted else ('—', 0)
        top_str = f"{top[0]} ({top[1]})"
        all_top5 = ' | '.join(f"{name} ×{n}" for name, n in prods_sorted[:5])
        aov = round(b['revenue'] / b['orders'], 0) if b['orders'] else 0
        rows.append([
            portal, campaign, b['utm_source'], b['utm_medium'],
            b['orders'], round(b['revenue'], 2), aov,
            top_str, len(b['product_units']), all_top5,
            (b['first_seen'] or '')[:19].replace('T', ' '),
            (b['last_seen']  or '')[:19].replace('T', ' '),
        ])
    return rows


def build_detail_rows(agg):
    """One row per (portal, campaign, product): units, revenue."""
    headers = ['Portal', 'UTM Campaign', 'UTM Source', 'Product', 'Units', 'Revenue (₹)']
    rows = [headers]
    items = sorted(agg.items(), key=lambda kv: (kv[0][0], -kv[1]['revenue']))
    for (portal, campaign), b in items:
        prods = sorted(b['product_units'].items(), key=lambda x: -x[1])
        for product, units in prods:
            rev = b['product_revenue'].get(product, 0.0)
            rows.append([portal, campaign, b['utm_source'], product, units, round(rev, 2)])
    return rows


def write_tab(sh, title, rows):
    """Create or replace a worksheet with the given rows."""
    cols = max(len(r) for r in rows) if rows else 12
    try:
        ws = sh.worksheet(title)
        ws.clear()
        ws.resize(rows=max(2, len(rows)), cols=cols)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=title, rows=max(100, len(rows)+10), cols=cols)
    ws.update('A1', rows, value_input_option='USER_ENTERED')
    # Move the new tab to the front
    sh.reorder_worksheets([ws] + [w for w in sh.worksheets() if w.title != title])
    print(f"  ✅ Wrote {len(rows)-1} data rows to '{title}'")


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--days', type=int, default=7,
                   help='Number of days back from today (IST). Default 7.')
    p.add_argument('--since', help='YYYY-MM-DD (overrides --days)')
    p.add_argument('--until', help='YYYY-MM-DD (overrides --days; inclusive)')
    p.add_argument('--portal', choices=['SM', 'SML', 'NBP'], help='Limit to one portal')
    args = p.parse_args()

    now_ist = datetime.now(IST)
    if args.since:
        since_dt = datetime.fromisoformat(args.since).replace(tzinfo=IST)
    else:
        since_dt = (now_ist - timedelta(days=args.days)).replace(hour=0, minute=0, second=0, microsecond=0)
    if args.until:
        # End of day IST
        until_dt = datetime.fromisoformat(args.until).replace(hour=23, minute=59, second=59, tzinfo=IST)
    else:
        until_dt = now_ist

    since_iso = since_dt.isoformat()
    until_iso = until_dt.isoformat()
    label_today = now_ist.strftime('%Y-%m-%d')

    print(f"📦 Shopify UTM Orders Report")
    print(f"   Range: {since_iso}  →  {until_iso}")
    print(f"   Sheet: {SHEET_ID}")

    portals = [args.portal] if args.portal else list(PORTAL_SHOPIFY.keys())
    all_agg = {}
    for portal in portals:
        print(f"\n→ {portal}")
        orders = fetch_orders(portal, since_iso, until_iso)
        print(f"   {portal}: {len(orders)} orders fetched")
        agg = aggregate(orders, portal)
        all_agg.update(agg)

    if not all_agg:
        print("\n⚠️  No orders found in range — nothing to write.")
        return

    # Build rows
    summary_rows = build_summary_rows(all_agg)
    detail_rows  = build_detail_rows(all_agg)

    # Connect to sheet
    creds = Credentials.from_service_account_file(SA_FILE, scopes=SCOPES)
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(SHEET_ID)

    summary_title = f'📦 UTM Orders Summary {label_today}'
    detail_title  = f'📦 UTM Orders Detail {label_today}'
    write_tab(sh, summary_title, summary_rows)
    write_tab(sh, detail_title,  detail_rows)

    # Print quick top-10 summary to stdout for the workflow log
    print("\n📊 Top 10 campaigns by revenue:")
    print(f"   {'Portal':<5} {'Campaign':<55} {'Orders':>7} {'Revenue':>12} {'Top Product':<40}")
    items = sorted(all_agg.items(), key=lambda kv: -kv[1]['revenue'])[:10]
    for (portal, camp), b in items:
        prods = sorted(b['product_units'].items(), key=lambda x: -x[1])
        top = f"{prods[0][0][:35]} ×{prods[0][1]}" if prods else '—'
        print(f"   {portal:<5} {camp[:55]:<55} {b['orders']:>7} ₹{b['revenue']:>10,.0f} {top:<40}")

    print(f"\n✅ Done. Tabs written: '{summary_title}' + '{detail_title}'")


if __name__ == '__main__':
    main()
