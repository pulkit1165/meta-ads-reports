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
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _utils import db_connect, IST, DEFAULT_DB, now_iso  # noqa: E402

SCHEMA = Path(__file__).resolve().parent / 'db_schema.sql'

# ── Product-title keyword fallback ───────────────────────────────────────────
# Only applied to SKUs MISSING from product_ntn_labels (never overrides a real
# label). Order matters — the first matching rule wins. The gold-tone rule is
# first so the new gold-jewellery range (VELORE / Golden chains + the gold-tone
# crystal pendants, all titled "…24K/18K Gold Tone Plated") lands in
# 24K Jewellery rather than Crystal Home Decor, which is what the operator
# expects. Operator should still backfill these SKUs into the master sheet; this
# only keeps the dashboard honest until then.
_FALLBACK_RULES = [
    ('24K Jewellery',     r'gold tone|gold-tone|gold plated|gold-plated|18k|24k gold|24kt|gold chain'),
    ('Nutraceuticals',    r'capsule|tablet|veg cap|kapikachu|ashwagandha|gummies|sachet|\bpowder\b'),
    ('Perfumes',          r'perfume|fragrance|eau de|\bedp\b|\bedt\b|attar|cologne'),
    ('Crystal Home Decor', r'crystal|pyrite|selenite|amethyst|quartz|aventurine|tourmaline|money bowl|geode|cluster|\btree\b|obsidian|citrine|\bjade\b'),
    ('24K Jewellery',     r'chain|pendant|bracelet|necklace|\bring\b|earring|anklet'),
]
_FALLBACK_RULES = [(cat, re.compile(rx)) for cat, rx in _FALLBACK_RULES]


def fallback_category(title):
    """Infer a category from a product title for SKUs not in the master sheet.
    Returns None if nothing matches (caller buckets these as 'Other')."""
    t = (title or '').lower()
    for cat, rx in _FALLBACK_RULES:
        if rx.search(t):
            return cat
    return None


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


def build_category(conn, since, until):
    """Aggregate REAL Shopify line-item revenue into antariksh_category_daily.

    Maps every line's SKU (= NTN code) to a category: first via the master sheet
    (product_ntn_labels), then via a product-title keyword fallback for SKUs the
    sheet is missing. The `source` column records which path resolved each row so
    the dashboard can show coverage.
    """
    conn.execute('DELETE FROM antariksh_category_daily WHERE date BETWEEN ? AND ?',
                 (since, until))

    # 1) Authoritative SKU → category from the master sheet (non-empty only).
    labels = {
        sku: cat for sku, cat in conn.execute(
            "SELECT ntn_code, TRIM(category) FROM product_ntn_labels "
            "WHERE TRIM(COALESCE(category,'')) != ''"
        ).fetchall()
    }

    # 2) Resolve every (sku, product_title) seen in-window → (category, source).
    #    Same SKU can carry different titles, so resolve at that grain; a real
    #    label always wins, title fallback only fills the gaps.
    pairs = conn.execute('''
        SELECT DISTINCT oi.sku, oi.product_title
        FROM shopify_order_items oi
        JOIN shopify_orders o
          ON oi.order_id = o.order_id AND oi.portal = o.portal
        WHERE substr(o.created_at, 1, 10) BETWEEN ? AND ?
          AND o.cancelled_at IS NULL
    ''', (since, until)).fetchall()

    conn.execute('DROP TABLE IF EXISTS _sku_cat_tmp')
    conn.execute('CREATE TEMP TABLE _sku_cat_tmp '
                 '(sku TEXT, product_title TEXT, category TEXT, source TEXT)')
    resolved = []
    for sku, title in pairs:
        if sku in labels:
            resolved.append((sku, title, labels[sku], 'sheet'))
        else:
            cat = fallback_category(title)
            if cat:
                resolved.append((sku, title, cat, 'name'))
            else:
                resolved.append((sku, title, 'Other', 'none'))
    conn.executemany(
        'INSERT INTO _sku_cat_tmp (sku, product_title, category, source) '
        'VALUES (?,?,?,?)', resolved)

    # 3) Aggregate line revenue/units/orders by date/portal/category/source.
    #    Join the temp table on BOTH sku and title so multi-title SKUs map right.
    rows = conn.execute('''
        SELECT
          substr(o.created_at, 1, 10)             AS date,
          oi.portal                                AS portal,
          t.category                               AS category,
          t.source                                 AS source,
          ROUND(SUM(COALESCE(oi.line_revenue, 0)), 2) AS revenue,
          SUM(COALESCE(oi.quantity, 0))            AS units,
          COUNT(DISTINCT oi.order_id)              AS orders
        FROM shopify_order_items oi
        JOIN shopify_orders o
          ON oi.order_id = o.order_id AND oi.portal = o.portal
        JOIN _sku_cat_tmp t
          ON oi.sku = t.sku
         AND COALESCE(oi.product_title,'') = COALESCE(t.product_title,'')
        WHERE substr(o.created_at, 1, 10) BETWEEN ? AND ?
          AND o.cancelled_at IS NULL
        GROUP BY substr(o.created_at, 1, 10), oi.portal, t.category, t.source
    ''', (since, until)).fetchall()
    conn.executemany('''
        INSERT OR REPLACE INTO antariksh_category_daily
          (date, portal, category, source, revenue, units, orders)
        VALUES (?,?,?,?,?,?,?)
    ''', rows)
    conn.execute('DROP TABLE IF EXISTS _sku_cat_tmp')
    return len(rows)


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
    n_cat = build_category(conn, since, until)

    # quick provenance line
    span = conn.execute(
        'SELECT MIN(date), MAX(date) FROM antariksh_daily').fetchone()
    print(f"antariksh rollup rebuilt [{since} .. {until}] @ {now_iso()}")
    print(f"  antariksh_daily          : {n_meta} rows  (span {span[0]}..{span[1]})")
    print(f"  antariksh_shopify_daily  : {n_shop} rows")
    print(f"  antariksh_category_daily : {n_cat} rows")
    conn.close()


if __name__ == '__main__':
    main()
