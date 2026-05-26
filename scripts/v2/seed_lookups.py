#!/usr/bin/env python3
"""
Seed the lookup tables (sentiment_labels, product_ntn_labels).

Idempotent — UPSERT on code/ntn_code so re-running keeps user edits intact.
The label/product columns use COALESCE on the existing value, so once a
user fills in a meaning it won't get overwritten on re-seed.

Usage:
  python3 scripts/v2/seed_lookups.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _utils import db_connect, now_iso


# NTN codes from reference_ntn_codes.md memory.
# (code, product, category) — keep in sync with that file.
NTN_SEED = [
    ('NTN117', 'Solid Perfume',                      'Perfumes'),
    ('NTN180', 'Brow Grow',                          'Skin'),
    ('NTN182', None,                                 'Skin'),
    ('NTN198', 'Goat Milk Serum',                    'Skin'),
    ('NTN211', 'Selenite',                           'Crystal'),
    ('NTN212', 'Selenite Recharging Plate',          'Crystal'),
    ('NTN213', None,                                 'Skin'),
    ('NTN221', 'Time Reversal',                      'Skin'),
    ('NTN222', 'Richie Rich',                        'Crystal'),
    ('NTN237', 'AM/PM Pigmentation Combo',           'Skin'),
    ('NTN248', None,                                 'Skin'),
    ('NTN274', None,                                 'Skin'),
    ('NTN275', 'Nagarmotha Oil',                     'Hair'),
    ('NTN294', 'Berberine',                          'Nutraceuticals'),
    ('NTN301', 'Xtreme Hair Growth Oil',             'Hair'),
    ('NTN302', 'Pigmentation Cream',                 'Skin'),
    ('NTN307', 'Lip Bright',                         'Skin'),
    ('NTN309', None,                                 'Skin'),
    ('NTN313', 'Inside Out',                         'Hair'),
    ('NTN328', None,                                 'Skin'),
    ('NTN351', 'Xtreme Booster Kit',                 'Hair'),
    ('NTN402', 'Sun Mousse',                         'Skin'),
    ('NTN429', 'Berberine variant',                  'Nutraceuticals'),
    ('NTN549', 'Money Bowl',                         'Crystal'),
    ('NTN555', 'Trifecta',                           'Skin'),
    ('NTN643', 'Solid Perfume (variant)',            'Perfumes'),
    ('NTN760', 'Phusphus',                           'Hair'),
    ('NTN761', 'Phusphus Combo',                     'Hair'),
    ('NTN801', 'Lapis Peacock',                      'Crystal'),
    ('NTN807', 'Pyrite Owl',                         'Crystal'),
    ('NTN808', 'Horses of Harmony',                  'Crystal'),
    ('NTN815', 'Richie Rich Half-n-Half Bracelet',   'Crystal'),
    ('NTN819', 'Horses',                             'Crystal'),
    ('NTN822', '7 Rainbow Horses',                   'Crystal'),
    ('NTN844', None,                                 'Crystal'),
    ('NTN892', None,                                 'Crystal'),
    ('NTN893', 'Owl',                                'Crystal'),
    ('NTN898', None,                                 'Crystal'),
    ('NTN932', 'Pyrite-Lapis Peacock Frame',         'Crystal'),
    ('NTN933', 'Pyrite',                             'Crystal'),
    ('NTN976', 'Triderma',                           'Skin'),
    ('NTN1018', 'CharbiGone',                        'Nutraceuticals'),
    ('NTN1079', 'Cheetah Clock',                     'Crystal'),
]


# Sentiment taxonomy — only these 4 codes are valid. Keep in sync with
# ALLOWED_SENTIMENTS in classify_ads.py. Anything outside this set found
# in an ad name (e.g. _st9_) is ignored by classify_ads → renders as
# (unset) on the Sentiments page.
SENTIMENT_SEED = [
    ('st1', 'Style/Design + Quality + Crystal Energy'),
    ('st2', 'Unisex Products + Quality + Crystal Energy'),
    ('st3', 'OG Gold Price Fear'),
    ('st4', 'Animal Storyline + Crystal Energy + Quality'),
]
SENTIMENT_CODES = [code for code, _ in SENTIMENT_SEED]
SENTIMENT_LABEL_MAP = dict(SENTIMENT_SEED)


def seed(conn):
    ts = now_iso()

    # NTN codes — UPSERT but PRESERVE user edits to product name
    n_ntn = 0
    for code, product, category in NTN_SEED:
        conn.execute(
            '''INSERT INTO product_ntn_labels(ntn_code, product, category, updated_at)
               VALUES(?, ?, ?, ?)
               ON CONFLICT(ntn_code) DO UPDATE SET
                 product = COALESCE(product_ntn_labels.product, excluded.product),
                 category = COALESCE(product_ntn_labels.category, excluded.category),
                 updated_at = excluded.updated_at''',
            (code, product, category, ts)
        )
        n_ntn += 1

    # Sentiment slots — UPSERT the 4 valid codes with labels. COALESCE
    # preserves any label set manually in the DB after seeding.
    n_sent = 0
    for code in SENTIMENT_CODES:
        seed_label = SENTIMENT_LABEL_MAP.get(code)
        conn.execute(
            '''INSERT INTO sentiment_labels(code, label, created_at, updated_at)
               VALUES(?, ?, ?, ?)
               ON CONFLICT(code) DO UPDATE SET
                 label = COALESCE(sentiment_labels.label, excluded.label),
                 updated_at = excluded.updated_at''',
            (code, seed_label, ts, ts)
        )
        n_sent += 1

    # Drop any sentiment codes outside the current taxonomy. These were
    # seeded as empty placeholders (st5..st20) in earlier versions and
    # serve no purpose now that classify_ads rejects non-allowed codes.
    placeholders = ','.join(['?'] * len(SENTIMENT_CODES))
    n_dropped = conn.execute(
        f'DELETE FROM sentiment_labels WHERE code NOT IN ({placeholders})',
        SENTIMENT_CODES,
    ).rowcount
    # And blank out the sentiment field for any ad that's still pointing
    # at a now-invalid code, so the dashboard doesn't show ghost buckets.
    n_unset_ads = conn.execute(
        f'''UPDATE meta_ads_meta
            SET sentiment = NULL, updated_at = ?
            WHERE sentiment IS NOT NULL
              AND sentiment NOT IN ({placeholders})''',
        [ts] + SENTIMENT_CODES,
    ).rowcount

    print(f"✅ Seeded {n_ntn} NTN codes")
    print(f"✅ Seeded {n_sent} sentiment codes ({', '.join(SENTIMENT_CODES)})")
    if n_dropped:
        print(f"🗑  Removed {n_dropped} out-of-taxonomy sentiment_labels rows")
    if n_unset_ads:
        print(f"🗑  Cleared sentiment on {n_unset_ads} ads using invalid codes")
    print()
    # Show count of NTN codes still unmapped (product=NULL)
    unmapped = conn.execute(
        "SELECT COUNT(*) FROM product_ntn_labels WHERE product IS NULL"
    ).fetchone()[0]
    if unmapped:
        print(f"📋 {unmapped} NTN codes have no product name yet — fill via:")
        print(f"   UPDATE product_ntn_labels SET product='X' WHERE ntn_code='NTN###';")
    print(f"\nFill sentiment labels via:")
    print(f"   UPDATE sentiment_labels SET label='Problem-Solution' WHERE code='st1';")


def main():
    conn = db_connect()
    seed(conn)
    conn.close()


if __name__ == '__main__':
    main()
