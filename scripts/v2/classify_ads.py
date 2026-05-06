#!/usr/bin/env python3
"""
NTN Dashboard v2 — classify ads.

Reads meta_ads_meta rows, extracts classifications from ad/campaign names
via regex, writes them back to:
  - category       (Skin/Hair/Crystal HD/Crystal Acc/Jewellery/Perfumes/Aibot/Nutra/Other)
  - creative_type  (Paras/Static/Motion/Partnership/AI/Other)
  - sentiment      (st1, st2, ... — raw codes; dashboard joins sentiment_labels)
  - ntn_code       (NTN237 etc.)
  - product        (looked up via product_ntn_labels join)

Idempotent — safe to re-run. Classification rules use a version number; we
re-classify any row where classification_version < CURRENT_VERSION so a rule
update propagates cleanly.

Usage:
  python3 scripts/v2/classify_ads.py                 # classify all unclassified
  python3 scripts/v2/classify_ads.py --reclassify    # force re-classify everyone
  python3 scripts/v2/classify_ads.py --ad-id 12345   # single ad
"""

import argparse
import re
import sys
from pathlib import Path

# Need product_catalogue.derive_category_v2 from existing scripts/
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _utils import db_connect, log_ingest_start, log_ingest_finish  # noqa: E402
from product_catalogue import derive_category_v2  # noqa: E402

# Bump this when classification rules change — forces re-classification on next run
CLASSIFICATION_VERSION = 3   # v3: expanded brand keywords + NTN sheet sync

# ── Regex patterns ────────────────────────────────────────────────────────
# NTN code: matches NTN1234, ntn1234 (case-insensitive). Word boundary on both
# sides prevents matching mid-word. We allow both 'ntn222' and 'ntn_222' just
# in case nomenclature drifts.
NTN_PATTERN = re.compile(r'(?<![A-Za-z0-9])(NTN_?(\d{2,5}))(?![A-Za-z0-9])', re.IGNORECASE)

# Sentiment: _st1_, _st12_, etc. Anchored to underscores to avoid matching
# things like "static" (which has 'st' but not as a code).
SENTIMENT_PATTERN = re.compile(r'(?<![A-Za-z0-9])(st(\d{1,3}))(?![A-Za-z0-9])', re.IGNORECASE)

# Creative type — order matters, first match wins. Each rule is a list of
# keywords. A keyword matches when it appears as a token (preceded/followed
# by underscore OR start/end of string) — handles `paras_x`, `x_paras`,
# `x_paras_y`, and bare `paras`. Avoids false matches inside longer words.
CREATIVE_TYPE_RULES = [
    ('Paras',       ['paras']),
    ('Wanda',       ['wanda']),                   # User's AI generator team
    ('Partnership', ['partnership', 'collab', 'creator']),
    ('AI',          ['ai_bot', 'aibot', 'ai_generated', 'ai']),
    ('Motion',      ['motion', 'reel', 'video']),
    ('Static',      ['static', 'image', 'post', 'carousel']),
]


def _has_token(text: str, keyword: str) -> bool:
    """True if `keyword` appears delimited by underscore or string boundary.
    Matches: 'static_x', 'x_static', 'x_static_y', '_static', 'static_'.
    Skips:   'staticness' (no boundary).
    Case-insensitive (assumes text already lowercased)."""
    # Pattern: (start or underscore) keyword (end or underscore)
    return re.search(rf'(?:^|_){re.escape(keyword)}(?:$|_)', text) is not None


def extract_ntn_code(text: str) -> str | None:
    """Returns 'NTN237' (uppercased, no underscore) or None."""
    if not text: return None
    m = NTN_PATTERN.search(text)
    if not m: return None
    digits = m.group(2)
    return f'NTN{digits}'


def extract_sentiment(text: str) -> str | None:
    """Returns 'st1' / 'st2' / etc. (lowercased) or None."""
    if not text: return None
    m = SENTIMENT_PATTERN.search(text)
    if not m: return None
    digits = m.group(2)
    return f'st{digits}'


def _match_creative_type(text: str) -> str | None:
    """Returns label or None (caller decides Other fallback).
    Token-aware match — keyword must be underscore-delimited or at boundary."""
    if not text: return None
    t = text.lower()
    for label, keywords in CREATIVE_TYPE_RULES:
        if any(_has_token(t, kw) for kw in keywords):
            return label
    return None


def extract_creative_type(ad_name: str, campaign_name: str) -> str:
    """Creative type — ad_name takes priority because campaign names have
    `_paras_` as a default filler in nearly every campaign, which would
    drown out the actual creative format on the ad."""
    # 1. Try ad_name first (most accurate signal)
    label = _match_creative_type(ad_name)
    if label: return label
    # 2. Fall back to campaign_name only if ad_name had no tag
    label = _match_creative_type(campaign_name)
    if label: return label
    return 'Other'


def derive_category_with_ntn(ad_name: str, campaign_name: str,
                             ntn_code: str | None,
                             ntn_to_category: dict) -> str:
    """Category resolution priority:
       1. NTN code → product_ntn_labels.category (most reliable: maps to actual SKU)
       2. Keyword-based derive_category_v2 on combined text
    """
    if ntn_code and ntn_to_category.get(ntn_code):
        # Map our SKU-level categories to dashboard categories.
        # 'Crystal' from product_ntn_labels splits in derive_category_v2
        # into 'Crystal Home Decor' / 'Crystal Accessory' — refine with keywords.
        cat = ntn_to_category[ntn_code]
        if cat == 'Crystal':
            # Use keyword pass to disambiguate Home Decor vs Accessory
            kw_cat = derive_category_v2(f'{ad_name or ""} {campaign_name or ""}')
            if kw_cat in ('Crystal Home Decor', 'Crystal Accessory'):
                return kw_cat
            return 'Crystal Home Decor'  # default crystal bucket
        return cat
    # Fall back to keyword-only classification
    combined = f"{campaign_name or ''} {ad_name or ''}"
    return derive_category_v2(combined)


def classify_one(ad_name: str, campaign_name: str,
                 ntn_to_category: dict = None) -> dict:
    """Returns dict of all derived fields for one ad."""
    ntn_to_category = ntn_to_category or {}
    combined = f"{ad_name or ''} {campaign_name or ''}"
    ntn_code = extract_ntn_code(combined)
    return {
        'category':       derive_category_with_ntn(ad_name, campaign_name,
                                                   ntn_code, ntn_to_category),
        'creative_type':  extract_creative_type(ad_name, campaign_name),
        'sentiment':      extract_sentiment(combined),
        'ntn_code':       ntn_code,
    }


def classify_all(conn, *, reclassify: bool = False, single_ad_id: str = None):
    """Walks meta_ads_meta and updates classifications.
    `reclassify=True` ignores classification_version and re-runs everyone.
    """
    where = []
    args  = []
    if single_ad_id:
        where.append('ad_id = ?')
        args.append(single_ad_id)
    elif not reclassify:
        where.append('(classification_version IS NULL OR classification_version < ?)')
        args.append(CLASSIFICATION_VERSION)
    sql = (
        'SELECT ad_id, ad_name, campaign_id FROM meta_ads_meta '
        + ('WHERE ' + ' AND '.join(where) if where else '')
    )
    ads = conn.execute(sql, args).fetchall()
    if not ads:
        print("No ads to classify.")
        return 0

    # Pre-load campaign names so we don't N+1 query
    camp_names = dict(conn.execute(
        'SELECT campaign_id, name FROM meta_campaigns'
    ).fetchall())

    # Pre-load product + category lookup from NTN labels
    ntn_lookup_rows = conn.execute(
        'SELECT ntn_code, product, category FROM product_ntn_labels WHERE is_active = 1'
    ).fetchall()
    ntn_to_product  = {r[0]: r[1] for r in ntn_lookup_rows if r[1]}
    ntn_to_category = {r[0]: r[2] for r in ntn_lookup_rows if r[2]}

    print(f"Classifying {len(ads)} ad(s)...")
    rows = []
    counts = {
        'category': {}, 'creative_type': {}, 'sentiment_set': 0,
        'ntn_set': 0, 'product_set': 0,
    }
    for ad_id, ad_name, campaign_id in ads:
        cname = camp_names.get(campaign_id, '')
        cls = classify_one(ad_name, cname, ntn_to_category=ntn_to_category)
        product = ntn_to_product.get(cls['ntn_code']) if cls['ntn_code'] else None

        rows.append((
            cls['category'],
            cls['creative_type'],
            cls['sentiment'],
            cls['ntn_code'],
            product,
            CLASSIFICATION_VERSION,
            ad_id,
        ))
        counts['category'][cls['category']] = counts['category'].get(cls['category'], 0) + 1
        counts['creative_type'][cls['creative_type']] = counts['creative_type'].get(cls['creative_type'], 0) + 1
        if cls['sentiment']: counts['sentiment_set'] += 1
        if cls['ntn_code']:  counts['ntn_set'] += 1
        if product:          counts['product_set'] += 1

    conn.executemany(
        '''UPDATE meta_ads_meta SET
             category = ?,
             creative_type = ?,
             sentiment = ?,
             ntn_code = ?,
             product = ?,
             classification_version = ?
           WHERE ad_id = ?''',
        rows
    )

    # Print summary
    print(f"\n📊 Classification summary:")
    print(f"   Categories:")
    for k, v in sorted(counts['category'].items(), key=lambda x: -x[1]):
        print(f"     {k:25s} {v:>5}")
    print(f"   Creative types:")
    for k, v in sorted(counts['creative_type'].items(), key=lambda x: -x[1]):
        print(f"     {k:25s} {v:>5}")
    print(f"   Sentiment tagged:  {counts['sentiment_set']} of {len(ads)}")
    print(f"   NTN code found:    {counts['ntn_set']} of {len(ads)}")
    print(f"   Product mapped:    {counts['product_set']} of {len(ads)}")
    print(f"\n✅ classify_ads complete — {len(rows)} ads updated to v{CLASSIFICATION_VERSION}")
    return len(rows)


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--reclassify', action='store_true',
                   help='Re-classify every ad regardless of version')
    p.add_argument('--ad-id', help='Classify a single ad_id (for debugging)')
    p.add_argument('--db', help='SQLite path (default state/ntn.db)')
    args = p.parse_args()

    conn = db_connect(Path(args.db)) if args.db else db_connect()
    target = args.ad_id or ('reclassify' if args.reclassify else 'incremental')
    started = log_ingest_start(conn, 'classify_ads', target)
    try:
        n = classify_all(conn, reclassify=args.reclassify, single_ad_id=args.ad_id)
        log_ingest_finish(conn, 'classify_ads', target, started,
                          status='success', rows_written=n)
    except Exception as e:
        import traceback; traceback.print_exc()
        log_ingest_finish(conn, 'classify_ads', target, started,
                          status='failed', error_message=str(e)[:500])
        sys.exit(2)
    conn.close()


if __name__ == '__main__':
    main()
