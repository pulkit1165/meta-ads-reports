#!/usr/bin/env python3
"""
Antariksh rollup builder.

Populates the pre-aggregated tables the Antariksh dashboard reads from:
  - antariksh_daily          (Meta side, per date/portal/category/creative_type)
  - antariksh_shopify_daily  (Shopify ground truth, per date/portal)

Both are rebuilt idempotently for the requested window (DELETE the date range,
then re-INSERT from the raw tables), so re-runs and overlapping cron shots never
double-count.

Methodology (matches the v2 dashboard the operator already trusts):
  - spend is REAL Meta spend.
  - meta_purchases / meta_revenue are PIXEL-attributed; used only for the
    per-category / per-creative split, which Shopify orders can't provide
    reliably (only ~11% of orders carry a usable UTM).
  - Shopify orders + revenue are GROUND TRUTH (cancelled excluded), and drive
    the blended hero KPIs + Prepaid%.

Usage:
  python3 scripts/v2/build_antariksh_rollup.py                 # last 90 days
  python3 scripts/v2/build_antariksh_rollup.py --days 30
  python3 scripts/v2/build_antariksh_rollup.py --all
  python3 scripts/v2/build_antariksh_rollup.py --db /tmp/ntn.db --all
"""

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _utils import db_connect, IST, DEFAULT_DB, now_iso  # noqa: E402

SCHEMA = Path(__file__).resolve().parent / 'db_schema.sql'


def ensure_tables(conn):
    """Create the antariksh_* tables if missing (idempotent CREATE IF NOT EXISTS
    blocks are pulled straight from db_schema.sql)."""
    ddl = SCHEMA.read_text()
    # Only run the antariksh_* statements so we don't touch other tables.
    start = ddl.find('-- ── Antariksh dashboard rollups')
    conn.executescript(ddl[start:] if start != -1 else ddl)


def date_window(args):
    if args.all:
        return '0000-01-01', '9999-12-31'
    until = datetime.now(IST).date()
    since = until - timedelta(days=args.days - 1)
    return since.isoformat(), until.isoformat()


def build_meta(conn, since, until):
    """Aggregate Meta ad-days into antariksh_daily."""
    conn.execute('DELETE FROM antariksh_daily WHERE date BETWEEN ? AND ?',
                 (since, until))
    rows = conn.execute('''
        SELECT
          d.date,
          d.portal,
          COALESCE(NULLIF(TRIM(m.category), ''), 'Other')      AS category,
          COALESCE(NULLIF(TRIM(m.creative_type), ''), 'Other') AS creative_type,
          ROUND(SUM(COALESCE(d.spend, 0)), 2)        AS spend,
          SUM(COALESCE(d.impressions, 0))            AS impressions,
          SUM(COALESCE(d.clicks, 0))                 AS clicks,
          ROUND(SUM(COALESCE(d.purchases, 0)), 2)    AS meta_purchases,
          ROUND(SUM(COALESCE(d.revenue, 0)), 2)      AS meta_revenue,
          COUNT(DISTINCT CASE WHEN d.spend > 0 THEN d.ad_id END) AS ad_count
        FROM meta_ads_daily d
        LEFT JOIN meta_ads_meta m ON d.ad_id = m.ad_id
        WHERE d.date BETWEEN ? AND ?
        GROUP BY d.date, d.portal, category, creative_type
    ''', (since, until)).fetchall()
    conn.executemany('''
        INSERT OR REPLACE INTO antariksh_daily
          (date, portal, category, creative_type, spend, impressions, clicks,
           meta_purchases, meta_revenue, ad_count)
        VALUES (?,?,?,?,?,?,?,?,?,?)
    ''', rows)
    return len(rows)


def build_shopify(conn, since, until):
    """Aggregate Shopify orders into antariksh_shopify_daily (ground truth)."""
    conn.execute('DELETE FROM antariksh_shopify_daily WHERE date BETWEEN ? AND ?',
                 (since, until))
    rows = conn.execute('''
        SELECT
          portal,
          substr(created_at, 1, 10) AS date,
          COUNT(*)                                            AS orders,
          ROUND(SUM(COALESCE(total_price, 0)), 2)             AS revenue,
          SUM(CASE WHEN financial_status IN ('paid','partially_paid')
                   THEN 1 ELSE 0 END)                         AS prepaid_orders,
          SUM(CASE WHEN financial_status = 'pending'
                   THEN 1 ELSE 0 END)                         AS cod_orders
        FROM shopify_orders
        WHERE substr(created_at, 1, 10) BETWEEN ? AND ?
          AND cancelled_at IS NULL
        GROUP BY portal, substr(created_at, 1, 10)
    ''', (since, until)).fetchall()
    # reorder to table column order (date, portal, ...)
    out = [(r[1], r[0], r[2], r[3], r[4], r[5]) for r in rows]
    conn.executemany('''
        INSERT OR REPLACE INTO antariksh_shopify_daily
          (date, portal, orders, revenue, prepaid_orders, cod_orders)
        VALUES (?,?,?,?,?,?)
    ''', out)
    return len(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', default=str(DEFAULT_DB))
    ap.add_argument('--days', type=int, default=90)
    ap.add_argument('--all', action='store_true')
    args = ap.parse_args()

    conn = db_connect(Path(args.db))
    ensure_tables(conn)
    since, until = date_window(args)

    n_meta = build_meta(conn, since, until)
    n_shop = build_shopify(conn, since, until)

    # quick provenance line
    span = conn.execute(
        'SELECT MIN(date), MAX(date) FROM antariksh_daily').fetchone()
    print(f"antariksh rollup rebuilt [{since} .. {until}] @ {now_iso()}")
    print(f"  antariksh_daily         : {n_meta} rows  (span {span[0]}..{span[1]})")
    print(f"  antariksh_shopify_daily : {n_shop} rows")
    conn.close()


if __name__ == '__main__':
    main()
