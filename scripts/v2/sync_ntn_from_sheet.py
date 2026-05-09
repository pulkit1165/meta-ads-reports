#!/usr/bin/env python3
"""
NTN Dashboard v2 — sync product_ntn_labels from the master SKU sheet.

Pulls https://docs.google.com/spreadsheets/d/1vNEAv6isGq66Hb407JZPcrrpfBgp1jj_cv-xt9DGmR0/
(first tab) and upserts every NTN code → product → category mapping into
state/ntn.db.product_ntn_labels.

Idempotent + preserves user-overridden values: if a row in product_ntn_labels
has manual notes, they're kept on re-run.

Schema we expect in the sheet (auto-detects column names):
  - 'NTN code' / 'NTN' / 'Code' / 'SKU'
  - 'Product' / 'Product Name' / 'Name'
  - 'Category' / 'Cat'

Usage:
  python3 scripts/v2/sync_ntn_from_sheet.py
"""

import os
import re
import sys
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

import gspread
from google.oauth2.service_account import Credentials

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _utils import db_connect, now_iso, log_ingest_start, log_ingest_finish  # noqa: E402

REPO_ROOT  = Path(__file__).resolve().parent.parent.parent
SA_FILE    = os.environ.get('GOOGLE_SERVICE_ACCOUNT_FILE') or str(REPO_ROOT / 'google-service-account.json')
SHEET_ID   = '1vNEAv6isGq66Hb407JZPcrrpfBgp1jj_cv-xt9DGmR0'
SCOPES     = ['https://www.googleapis.com/auth/spreadsheets.readonly']

NTN_HEADERS      = ('NTN code', 'NTN Code', 'NTN', 'Code', 'SKU', 'SKU Code', 'sku')
PRODUCT_HEADERS  = ('Product', 'Product Name', 'Product name', 'Name', 'product')
CATEGORY_HEADERS = ('SKU CATEGORY', 'SKU_CATEGORY', 'Category', 'Cat', 'category', 'Type', 'Categorization')


def find_column_index(header_row, candidates):
    """Returns index of first matching column or -1."""
    norm_row = [h.strip() for h in header_row]
    for cand in candidates:
        if cand in norm_row:
            return norm_row.index(cand)
    # Case-insensitive fallback
    norm_lower = [h.strip().lower() for h in header_row]
    for cand in candidates:
        if cand.lower() in norm_lower:
            return norm_lower.index(cand.lower())
    return -1


def normalize_ntn_code(raw: str) -> str | None:
    """Coerce '237', 'NTN237', 'ntn237', 'ntn 237' → 'NTN237'."""
    if not raw: return None
    s = str(raw).strip()
    if not s: return None
    # Already starts with NTN
    m = re.match(r'^\s*NTN\s*[_\-\s]?(\d{2,5})\s*$', s, re.IGNORECASE)
    if m:
        return f'NTN{m.group(1)}'
    # Bare digits
    m = re.match(r'^\s*(\d{2,5})\s*$', s)
    if m:
        return f'NTN{m.group(1)}'
    # Contains NTN somewhere
    m = re.search(r'NTN\s*(\d{2,5})', s, re.IGNORECASE)
    if m:
        return f'NTN{m.group(1)}'
    return None


def normalize_category(raw: str) -> str | None:
    """Map free-text category to our canonical bucket.

    Returns Title-Case canonical buckets matching derive_category_v2 output:
    Skin, Hair, Crystal, 24K Jewellery, Perfumes, Aibot, Nutraceuticals, Other.

    The fallback used to return raw.strip() — which preserved sheet casing
    like 'SKIN CARE' or 'HAIR CARE' and split the dashboard's category chart
    into duplicate buckets ('Skin' + 'SKIN CARE'). Now the fallback also
    title-cases so unknown variants at least render as one bucket per name.
    """
    if not raw: return None
    s = str(raw).strip().lower()
    if not s: return None
    mapping = {
        'skin': 'Skin', 'skincare': 'Skin', 'skin care': 'Skin', 'face': 'Skin',
        'hair': 'Hair', 'haircare': 'Hair', 'hair care': 'Hair',
        'crystal': 'Crystal', 'crystals': 'Crystal',
        'crystal home decor': 'Crystal', 'home decor': 'Crystal',
        'crystal accessory': 'Crystal', 'accessory': 'Crystal',
        'jewellery': 'Crystal',
        '24k jewellery': '24K Jewellery', '24k': '24K Jewellery',
        'gold': '24K Jewellery', 'gold jewellery': '24K Jewellery',
        'perfume': 'Perfumes', 'perfumes': 'Perfumes', 'fragrance': 'Perfumes',
        'aibot': 'Aibot', 'ai bot': 'Aibot', 'ai': 'Aibot',
        'astro': 'Aibot', 'astrology': 'Aibot',
        'nutra': 'Nutraceuticals', 'nutraceutical': 'Nutraceuticals',
        'nutraceuticals': 'Nutraceuticals', 'supplements': 'Nutraceuticals',
        'capsules': 'Nutraceuticals',
        'other': 'Other', 'others': 'Other',
        'service': 'Other', 'services': 'Other',
        'clothing': 'Other', 'apparel': 'Other',
        'dental care': 'Other', 'dental': 'Other',
    }
    return mapping.get(s, raw.strip().title())


def fetch_sheet():
    """Returns list of dicts with keys: ntn_code, product, category."""
    creds = Credentials.from_service_account_file(SA_FILE, scopes=SCOPES)
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(SHEET_ID)
    ws = sh.get_worksheet(0)
    rows = ws.get_all_values()
    if not rows or len(rows) < 2:
        return []

    print(f"📄 Sheet: {sh.title} → tab '{ws.title}'")
    print(f"   Rows: {len(rows)} (incl. header)")

    header = rows[0]
    print(f"   Header: {header}")
    ntn_i = find_column_index(header, NTN_HEADERS)
    prod_i = find_column_index(header, PRODUCT_HEADERS)
    cat_i  = find_column_index(header, CATEGORY_HEADERS)
    print(f"   Column indexes: ntn={ntn_i}  product={prod_i}  category={cat_i}")

    if ntn_i < 0:
        print("   ❌ Could not find NTN code column. Header candidates:", NTN_HEADERS)
        return []

    out = []
    skipped = 0
    for r in rows[1:]:
        if not any(c.strip() for c in r):
            continue
        raw_code = r[ntn_i] if ntn_i < len(r) else ''
        code = normalize_ntn_code(raw_code)
        if not code:
            skipped += 1
            continue
        product = (r[prod_i] if prod_i >= 0 and prod_i < len(r) else '').strip() or None
        category_raw = (r[cat_i] if cat_i >= 0 and cat_i < len(r) else '').strip()
        category = normalize_category(category_raw) if category_raw else None
        out.append({'ntn_code': code, 'product': product, 'category': category})
    print(f"   Parsed: {len(out)} valid rows  (skipped {skipped} non-NTN rows)")
    return out


def upsert(conn, rows):
    """UPSERT each NTN row. Preserves user-edited fields via COALESCE on the
    existing-row side — i.e., the sheet wins for new mappings, but we don't
    blow away a manually-tuned product name with NULL from the sheet."""
    if not rows:
        return 0, 0
    ts = now_iso()
    n_new = 0; n_updated = 0
    for r in rows:
        before = conn.execute(
            'SELECT product, category FROM product_ntn_labels WHERE ntn_code = ?',
            (r['ntn_code'],)
        ).fetchone()
        conn.execute(
            '''INSERT INTO product_ntn_labels(ntn_code, product, category, updated_at)
               VALUES(?, ?, ?, ?)
               ON CONFLICT(ntn_code) DO UPDATE SET
                 product = COALESCE(excluded.product, product_ntn_labels.product),
                 category = COALESCE(excluded.category, product_ntn_labels.category),
                 updated_at = excluded.updated_at''',
            (r['ntn_code'], r['product'], r['category'], ts)
        )
        if before is None:
            n_new += 1
        elif (before[0] != r['product'] and r['product']) or (before[1] != r['category'] and r['category']):
            n_updated += 1
    return n_new, n_updated


def main():
    if not Path(SA_FILE).exists():
        print(f"❌ Service account file missing: {SA_FILE}")
        print("   Cannot read the sheet. Skipping (non-fatal).")
        sys.exit(0)

    conn = db_connect()
    started = log_ingest_start(conn, 'sync_ntn_from_sheet', datetime.now(ZoneInfo('Asia/Kolkata')).date().isoformat())
    try:
        rows = fetch_sheet()
        n_new, n_updated = upsert(conn, rows)

        # Backfill: normalize any pre-existing dirty casing in stored rows
        # (legacy rows from before normalize_category was strengthened —
        # 'SKIN CARE', 'HAIR CARE', 'OTHER' etc. that split the dashboard
        # category chart into duplicate buckets).
        existing = conn.execute(
            'SELECT ntn_code, category FROM product_ntn_labels WHERE category IS NOT NULL'
        ).fetchall()
        n_renormalized = 0
        for code, cat in existing:
            clean = normalize_category(cat)
            if clean and clean != cat:
                conn.execute(
                    'UPDATE product_ntn_labels SET category = ? WHERE ntn_code = ?',
                    (clean, code)
                )
                n_renormalized += 1
        # Also fix already-classified meta_ads_meta rows that picked up the
        # dirty category before this fix landed.
        meta_existing = conn.execute(
            'SELECT DISTINCT category FROM meta_ads_meta WHERE category IS NOT NULL'
        ).fetchall()
        n_meta_renormalized = 0
        for (cat,) in meta_existing:
            clean = normalize_category(cat)
            if clean and clean != cat:
                cur = conn.execute(
                    'UPDATE meta_ads_meta SET category = ? WHERE category = ?',
                    (clean, cat)
                )
                n_meta_renormalized += cur.rowcount or 0
        if n_renormalized or n_meta_renormalized:
            print(f"   Renormalized: {n_renormalized} NTN labels, {n_meta_renormalized} ad rows")
        conn.commit()
        # Print summary
        total = conn.execute('SELECT COUNT(*) FROM product_ntn_labels').fetchone()[0]
        with_product = conn.execute(
            "SELECT COUNT(*) FROM product_ntn_labels WHERE product IS NOT NULL"
        ).fetchone()[0]
        with_category = conn.execute(
            "SELECT COUNT(*) FROM product_ntn_labels WHERE category IS NOT NULL"
        ).fetchone()[0]
        print(f"\n✅ NTN sync complete")
        print(f"   New rows added: {n_new}")
        print(f"   Updated rows:   {n_updated}")
        print(f"   Total mapped:   {total}")
        print(f"   With product:   {with_product}")
        print(f"   With category:  {with_category}")
        log_ingest_finish(conn, 'sync_ntn_from_sheet',
                          datetime.now(ZoneInfo('Asia/Kolkata')).date().isoformat(),
                          started, status='success',
                          rows_written=n_new + n_updated)
    except Exception as e:
        import traceback; traceback.print_exc()
        log_ingest_finish(conn, 'sync_ntn_from_sheet',
                          datetime.now(ZoneInfo('Asia/Kolkata')).date().isoformat(),
                          started, status='failed',
                          error_message=str(e)[:500])
        # Non-fatal — sync failure shouldn't block dashboard build
        print(f"\n⚠️  Sync failed (non-fatal): {e}")
    conn.close()


if __name__ == '__main__':
    main()
