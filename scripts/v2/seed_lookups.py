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


# Sentiment taxonomy — operator's full list of 56 codes (4 legacy +
# 52 storyline-framework codes). Each ST10X family is a set of A-F
# permutations of the same 3 themes. Keep in sync with
# ALLOWED_SENTIMENTS in classify_ads.py and SENTIMENT_LABELS in
# build_dashboard.py (JS). Codes outside this set found in an ad name
# get ignored by classify_ads → render as (unset) on the Sentiments
# page.
SENTIMENT_SEED = [
    # Legacy 4 — crystal/jewellery storyline framings
    ('st1',    'Style/Design + Quality + Crystal Energy'),
    ('st2',    'Unisex Product + Quality + Crystal Energy'),
    ('st3',    'OG Gold Price Fear'),
    ('st4',    'Paras Storyline + Crystal Energy + Quality'),
    # ST101 — Quality / Achievement / Testing
    ('st101a', 'Quality + Achievement + Testing'),
    ('st101b', 'Quality + Testing + Achievement'),
    ('st101c', 'Achievement + Quality + Testing'),
    ('st101d', 'Achievement + Testing + Quality'),
    ('st101e', 'Testing + Quality + Achievement'),
    ('st101f', 'Testing + Achievement + Quality'),
    # ST102 — Fear-Based Problem / SKU as Solution / Validation
    ('st102a', 'Fear Based Problem + SKU as Solution + Validation'),
    ('st102b', 'Fear Based Problem + Validation + SKU as Solution'),
    ('st102c', 'SKU as Solution + Fear Based Problem + Validation'),
    ('st102d', 'SKU as Solution + Validation + Fear Based Problem'),
    ('st102e', 'Validation + Fear Based Problem + SKU as Solution'),
    ('st102f', 'Validation + SKU as Solution + Fear Based Problem'),
    # ST103 — Desire / Fast Results / Quality
    ('st103a', 'Desire + Fast Results + Quality'),
    ('st103b', 'Desire + Quality + Fast Results'),
    ('st103c', 'Fast Results + Desire + Quality'),
    ('st103d', 'Fast Results + Quality + Desire'),
    ('st103e', 'Quality + Desire + Fast Results'),
    ('st103f', 'Quality + Fast Results + Desire'),
    # ST104 — Before/After / Testimonial / Testing
    ('st104a', 'Before/After + Testimonial + Testing'),
    ('st104b', 'Before/After + Testing + Testimonial'),
    ('st104c', 'Testimonial + Before/After + Testing'),
    ('st104d', 'Testimonial + Testing + Before/After'),
    ('st104e', 'Testing + Before/After + Testimonial'),
    ('st104f', 'Testing + Testimonial + Before/After'),
    # ST105 — Relevance / Validation / Trust
    ('st105a', 'Relevance + Validation + Trust'),
    ('st105b', 'Relevance + Trust + Validation'),
    ('st105c', 'Validation + Relevance + Trust'),
    ('st105d', 'Validation + Trust + Relevance'),
    ('st105e', 'Trust + Relevance + Validation'),
    ('st105f', 'Trust + Validation + Relevance'),
    # ST106 — Ingredient Story / Benefits / Quality
    ('st106a', 'Ingredient Story + Benefits + Quality'),
    ('st106b', 'Ingredient Story + Quality + Benefits'),
    ('st106c', 'Benefits + Ingredient Story + Quality'),
    ('st106d', 'Benefits + Quality + Ingredient Story'),
    ('st106e', 'Quality + Ingredient Story + Benefits'),
    ('st106f', 'Quality + Benefits + Ingredient Story'),
    # ST107 — Manufacturing/R&D / Quality / Achievements
    ('st107a', 'Manufacturing/R&D + Quality + Achievements'),
    ('st107b', 'Manufacturing/R&D + Achievements + Quality'),
    ('st107c', 'Quality + Manufacturing/R&D + Achievements'),
    ('st107d', 'Quality + Achievements + Manufacturing/R&D'),
    ('st107e', 'Achievements + Manufacturing/R&D + Quality'),
    ('st107f', 'Achievements + Quality + Manufacturing/R&D'),
    # ST108 — singletons
    ('st108a', 'Offer / Deal'),
    # ST109 — Competitor Comparison / Achievements / Trust
    ('st109a', 'Competitor Comparison + Achievements + Trust'),
    ('st109b', 'Competitor Comparison + Trust + Achievements'),
    ('st109c', 'Achievements + Competitor Comparison + Trust'),
    ('st109d', 'Achievements + Trust + Competitor Comparison'),
    ('st109e', 'Trust + Competitor Comparison + Achievements'),
    ('st109f', 'Trust + Achievements + Competitor Comparison'),
    # ST110-112 — singletons
    ('st110a', 'Celebrity / Social Proof'),
    ('st111a', 'Daily Routine'),
    ('st112a', 'Launch Purpose + BTS + Usage'),
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
    # NOTE: meta_ads_meta has no updated_at column (per db_schema.sql),
    # so don't try to set one here — that's what was 500-ing v2-ingest
    # and leaving the dashboard with an empty payload.
    n_unset_ads = conn.execute(
        f'''UPDATE meta_ads_meta
            SET sentiment = NULL
            WHERE sentiment IS NOT NULL
              AND sentiment NOT IN ({placeholders})''',
        SENTIMENT_CODES,
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
