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


# Pre-create empty sentiment slots so the table has rows even before user
# tags any ads. User fills in label/description via SQL UPDATE or an admin
# UI later. We create st1..st20; extend with seed_lookups.py if more needed.
SENTIMENT_CODES = [f'st{i}' for i in range(1, 21)]


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

    # Sentiment placeholders — same idempotency rule
    n_sent = 0
    for code in SENTIMENT_CODES:
        conn.execute(
            '''INSERT INTO sentiment_labels(code, created_at, updated_at)
               VALUES(?, ?, ?)
               ON CONFLICT(code) DO UPDATE SET updated_at = excluded.updated_at''',
            (code, ts, ts)
        )
        n_sent += 1

    print(f"✅ Seeded {n_ntn} NTN codes")
    print(f"✅ Seeded {n_sent} sentiment placeholders ({SENTIMENT_CODES[0]}…{SENTIMENT_CODES[-1]})")
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
