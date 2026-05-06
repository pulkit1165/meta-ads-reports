#!/usr/bin/env python3
"""
NTN Dashboard v2 — Shopify ingestion into SQLite.

Pulls orders from all 3 Shopify stores (SM/SML/NBP) for a date range, parses
UTM params from landing_site (with note_attributes fallback), expands line
items into shopify_order_items. Idempotent, rate-limit aware, logs runs.

Usage:
  python3 scripts/v2/ingest_shopify.py             # last 2 days (default cron)
  python3 scripts/v2/ingest_shopify.py --days 7
  python3 scripts/v2/ingest_shopify.py --since 2026-04-25 --until 2026-05-04
  python3 scripts/v2/ingest_shopify.py --portal SM
"""

import argparse
import os
import sys
import time
import traceback
import requests
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _utils import (  # noqa: E402
    db_connect, IST, PORTAL_SHOPIFY, now_iso,
    safe_float, safe_int,
    log_ingest_start, log_ingest_finish,
)

API_VERSION = '2024-01'
PAGE_SLEEP_S = 0.6     # Shopify REST: 2 req/s burst, 40/min sustained


def parse_utm_from_url(url: str) -> dict:
    if not url: return {}
    try:
        qs = parse_qs(urlparse(url).query)
        out = {}
        for k in ('utm_source', 'utm_medium', 'utm_campaign',
                  'utm_content', 'utm_term'):
            v = qs.get(k, [''])[0].strip()
            if v: out[k] = v
        return out
    except Exception:
        return {}


def parse_utm_from_note_attrs(note_attrs):
    out = {}
    if not note_attrs: return out
    for a in note_attrs:
        name = (a.get('name') or '').strip().lower()
        if name.startswith('utm_'):
            v = (a.get('value') or '').strip()
            if v: out[name] = v
    return out


def extract_utm(order: dict) -> dict:
    utm = parse_utm_from_url(order.get('landing_site') or '')
    note_utm = parse_utm_from_note_attrs(order.get('note_attributes') or [])
    for k, v in note_utm.items():
        utm.setdefault(k, v)
    if 'utm_source' not in utm:
        ref = (order.get('referring_site') or '').lower()
        if 'facebook' in ref or 'fb.com' in ref:
            utm['utm_source'] = 'facebook'
        elif 'instagram' in ref:
            utm['utm_source'] = 'instagram'
    return utm


def fetch_orders(portal: str, since_iso: str, until_iso: str) -> list:
    url_var, tok_var = PORTAL_SHOPIFY[portal]
    url = os.environ.get(url_var)
    token = os.environ.get(tok_var)
    if not (url and token):
        print(f"  ⚠️  Shopify creds missing for {portal} — skipping")
        return []

    api = f'https://{url}/admin/api/{API_VERSION}/orders.json'
    params = {
        'created_at_min': since_iso,
        'created_at_max': until_iso,
        'limit': 250,
        'status': 'any',
        'fields': ','.join([
            'id', 'name', 'created_at', 'cancelled_at', 'financial_status',
            'total_price', 'subtotal_price', 'currency',
            'customer', 'landing_site', 'referring_site', 'source_name',
            'note_attributes', 'line_items',
        ]),
    }
    headers = {'X-Shopify-Access-Token': token}
    out = []
    page = 0
    next_url = api
    next_params = params

    while next_url:
        page += 1
        try:
            r = requests.get(next_url, params=next_params,
                             headers=headers, timeout=60)
        except requests.exceptions.RequestException as e:
            print(f"  ⚠️  {portal} page {page} request error: {e}")
            break
        if r.status_code == 429:
            wait_s = int(r.headers.get('Retry-After', '5'))
            print(f"  ⏳ {portal} rate-limited, sleep {wait_s}s")
            time.sleep(wait_s)
            continue
        if r.status_code != 200:
            print(f"  ⚠️  {portal} HTTP {r.status_code} on page {page}: "
                  f"{r.text[:200]}")
            break
        orders = r.json().get('orders', []) or []
        out.extend(orders)
        print(f"  {portal} page {page}: {len(orders)} orders "
              f"(running total {len(out)})")

        # Cursor pagination via Link header
        link = r.headers.get('Link', '')
        next_url = None
        next_params = None
        for part in link.split(','):
            if 'rel="next"' in part:
                seg = part.strip().split(';')[0].strip().strip('<>')
                next_url = seg
                break
        if next_url:
            time.sleep(PAGE_SLEEP_S)
    return out


def upsert_orders(conn, portal: str, orders: list):
    """Idempotent UPSERT for shopify_orders + shopify_order_items."""
    order_rows = []
    item_rows = []
    for o in orders:
        oid = str(o.get('id'))
        if not oid: continue
        utm = extract_utm(o)
        cust = o.get('customer') or {}
        order_rows.append((
            oid, portal,
            o.get('name'),
            o.get('created_at'),
            o.get('cancelled_at'),
            o.get('financial_status'),
            safe_float(o.get('total_price')),
            safe_float(o.get('subtotal_price')),
            o.get('currency'),
            str(cust.get('id')) if cust.get('id') else None,
            cust.get('email'),
            o.get('landing_site'),
            o.get('referring_site'),
            o.get('source_name'),
            utm.get('utm_source'),
            utm.get('utm_medium'),
            utm.get('utm_campaign'),
            utm.get('utm_content'),
            utm.get('utm_term'),
        ))
        for li in o.get('line_items') or []:
            line_id = str(li.get('id'))
            if not line_id: continue
            qty = safe_int(li.get('quantity'))
            price = safe_float(li.get('price'))
            item_rows.append((
                oid, portal, line_id,
                str(li.get('product_id')) if li.get('product_id') else None,
                str(li.get('variant_id')) if li.get('variant_id') else None,
                li.get('sku'),
                li.get('title'),
                li.get('variant_title'),
                qty, price,
                qty * price,
            ))

    if order_rows:
        conn.executemany(
            '''INSERT INTO shopify_orders
               (order_id, portal, order_number, created_at, cancelled_at,
                financial_status, total_price, subtotal_price, currency,
                customer_id, customer_email, landing_site, referring_site,
                source_name,
                utm_source, utm_medium, utm_campaign, utm_content, utm_term)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(order_id) DO UPDATE SET
                 portal=excluded.portal,
                 order_number=excluded.order_number,
                 created_at=excluded.created_at,
                 cancelled_at=excluded.cancelled_at,
                 financial_status=excluded.financial_status,
                 total_price=excluded.total_price,
                 subtotal_price=excluded.subtotal_price,
                 currency=excluded.currency,
                 customer_id=COALESCE(excluded.customer_id, shopify_orders.customer_id),
                 customer_email=COALESCE(excluded.customer_email, shopify_orders.customer_email),
                 landing_site=COALESCE(excluded.landing_site, shopify_orders.landing_site),
                 referring_site=COALESCE(excluded.referring_site, shopify_orders.referring_site),
                 source_name=COALESCE(excluded.source_name, shopify_orders.source_name),
                 utm_source=COALESCE(excluded.utm_source, shopify_orders.utm_source),
                 utm_medium=COALESCE(excluded.utm_medium, shopify_orders.utm_medium),
                 utm_campaign=COALESCE(excluded.utm_campaign, shopify_orders.utm_campaign),
                 utm_content=COALESCE(excluded.utm_content, shopify_orders.utm_content),
                 utm_term=COALESCE(excluded.utm_term, shopify_orders.utm_term)''',
            order_rows
        )

    if item_rows:
        conn.executemany(
            '''INSERT INTO shopify_order_items
               (order_id, portal, line_id, product_id, variant_id, sku,
                product_title, variant_title, quantity, price, line_revenue)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(order_id, line_id) DO UPDATE SET
                 portal=excluded.portal,
                 product_id=excluded.product_id,
                 variant_id=excluded.variant_id,
                 sku=excluded.sku,
                 product_title=excluded.product_title,
                 variant_title=excluded.variant_title,
                 quantity=excluded.quantity,
                 price=excluded.price,
                 line_revenue=excluded.line_revenue''',
            item_rows
        )

    return len(order_rows), len(item_rows)


def ingest_range(conn, portals: list, since_iso: str, until_iso: str,
                 target_label: str):
    started = log_ingest_start(conn, 'ingest_shopify', target_label)
    total_orders = 0
    total_items = 0
    try:
        for portal in portals:
            print(f"\n→ {portal}")
            orders = fetch_orders(portal, since_iso, until_iso)
            print(f"   {portal}: {len(orders)} orders fetched")
            n_orders, n_items = upsert_orders(conn, portal, orders)
            total_orders += n_orders
            total_items += n_items
            print(f"   {portal}: upserted {n_orders} orders, {n_items} line items")
        log_ingest_finish(conn, 'ingest_shopify', target_label, started,
                          status='success',
                          rows_written=total_orders + total_items)
        print(f"\n✅ ingest_shopify complete · {total_orders} orders · {total_items} line items")
        return True
    except Exception as e:
        traceback.print_exc()
        log_ingest_finish(conn, 'ingest_shopify', target_label, started,
                          status='failed',
                          rows_written=total_orders + total_items,
                          error_message=str(e)[:500])
        return False


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--days', type=int, default=2,
                   help='Days back from today IST (default 2 for daily cron)')
    p.add_argument('--since', help='YYYY-MM-DD (overrides --days)')
    p.add_argument('--until', help='YYYY-MM-DD (overrides --days)')
    p.add_argument('--portal', choices=['SM', 'SML', 'NBP'])
    p.add_argument('--db', help='SQLite path (default state/ntn.db)')
    args = p.parse_args()

    now = datetime.now(IST)
    if args.since:
        since_dt = datetime.strptime(args.since, '%Y-%m-%d').replace(tzinfo=IST)
    else:
        since_dt = (now - timedelta(days=args.days)).replace(
            hour=0, minute=0, second=0, microsecond=0)
    if args.until:
        until_dt = datetime.strptime(args.until, '%Y-%m-%d').replace(
            hour=23, minute=59, second=59, tzinfo=IST)
    else:
        until_dt = now

    portals = [args.portal] if args.portal else ['SM', 'SML', 'NBP']
    db_path = Path(args.db) if args.db else None
    conn = db_connect(db_path) if db_path else db_connect()

    print(f"📥 Shopify Ingest")
    print(f"   Range: {since_dt.isoformat()}  →  {until_dt.isoformat()}")
    print(f"   Portals: {portals}")

    target_label = f"{since_dt.date()}_{until_dt.date()}"
    ok = ingest_range(conn, portals, since_dt.isoformat(),
                      until_dt.isoformat(), target_label)
    conn.close()
    sys.exit(0 if ok else 2)


if __name__ == '__main__':
    main()
