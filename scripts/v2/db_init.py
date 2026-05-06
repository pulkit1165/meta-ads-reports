#!/usr/bin/env python3
"""
NTN Dashboard v2 — initialize SQLite database from schema.

Idempotent — safe to run repeatedly. Creates state/ntn.db if missing,
applies any new tables/indexes from db_schema.sql.

Usage:
  python3 scripts/v2/db_init.py
  python3 scripts/v2/db_init.py --db /custom/path.db
  python3 scripts/v2/db_init.py --verify     # only check existing schema
"""

import argparse, sqlite3, sys
from pathlib import Path

REPO_ROOT  = Path(__file__).resolve().parent.parent.parent
SCHEMA_SQL = Path(__file__).resolve().parent / 'db_schema.sql'
DEFAULT_DB = REPO_ROOT / 'state' / 'ntn.db'


def init_db(db_path: Path):
    """Apply schema. CREATE IF NOT EXISTS makes this idempotent."""
    if not SCHEMA_SQL.exists():
        print(f"❌ Schema file missing: {SCHEMA_SQL}")
        sys.exit(1)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    schema = SCHEMA_SQL.read_text()
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(schema)
        conn.commit()
        # Inventory what's there now
        rows = conn.execute(
            "SELECT name, type FROM sqlite_master "
            "WHERE type IN ('table','index') AND name NOT LIKE 'sqlite_%' "
            "ORDER BY type, name"
        ).fetchall()
        tables  = [r[0] for r in rows if r[1] == 'table']
        indexes = [r[0] for r in rows if r[1] == 'index']
        print(f"✅ DB initialized at {db_path}")
        print(f"   Tables ({len(tables)}): {', '.join(tables)}")
        print(f"   Indexes ({len(indexes)})")
        # Row counts (so we can see what's already populated)
        print()
        print("   Current row counts:")
        for t in tables:
            n = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            print(f"     {t:30s} {n:>10,}")
    finally:
        conn.close()


def verify_db(db_path: Path):
    """Check existing schema matches what's in the SQL file."""
    if not db_path.exists():
        print(f"❌ DB doesn't exist: {db_path}")
        sys.exit(1)
    conn = sqlite3.connect(str(db_path))
    try:
        # Required tables
        required = {
            'meta_ads_daily', 'meta_ads_meta', 'meta_campaigns',
            'shopify_orders', 'shopify_order_items',
            'kpi_daily_rollup', 'ingest_log',
        }
        existing = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()}
        missing = required - existing
        extra   = existing - required
        if missing:
            print(f"❌ Missing tables: {missing}")
            sys.exit(2)
        print(f"✅ All {len(required)} required tables present")
        if extra:
            print(f"   (extra tables also present: {extra})")
    finally:
        conn.close()


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--db', default=str(DEFAULT_DB), help='Path to SQLite file')
    p.add_argument('--verify', action='store_true', help='Only verify existing schema')
    args = p.parse_args()

    db_path = Path(args.db)
    if args.verify:
        verify_db(db_path)
    else:
        init_db(db_path)


if __name__ == '__main__':
    main()
