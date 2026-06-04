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
CLASSIFICATION_VERSION = 4   # v4: Path B — product name matcher (ad name → NTN code)


# Words that appear in product names but are too generic to match on
# (would cause false positives across many ads).
GENERIC_TOKEN_BLACKLIST = {
    'skin', 'hair', 'perfume', 'crystal', 'ad', 'sale', 'sales',
    'retarget', 'reel', 'clp', 'ntn', 'paras', 'wanda', 'motion',
    'static', 'partnership', 'inde', 'web', 'conv', 'combo',
    'pack', 'set', 'kit', 'bundle', 'gift', 'free', 'new',
    'special', 'edition', 'series', 'range', 'collection', 'pro',
    'plus', 'mini', 'large', 'small', 'big', 'oil', 'cream',
    'serum', 'mask', 'wash', 'gel', 'lotion', 'mist', 'spray',
    'bracelet', 'pendant', 'plate', 'frame', 'clock', 'horse',
    'tree', 'sphere', 'wand', 'leaf', 'tower', 'cluster',
    'pyrite', 'selenite', 'amethyst', 'rose_quartz', 'citrine',
    'obsidian', 'tigereye', 'opalite', 'hematite',
    'face', 'lip', 'eye', 'body', 'scrub', 'powder', 'tablet',
    'capsule', 'capsules', 'st1', 'st2', 'st3', 'st4', 'st5',
}

MIN_KEYWORD_LEN = 5     # below this, too noisy to match


def _normalize_product_to_keywords(product_name: str) -> list:
    """Convert a product name from the SKU sheet into matching keywords.
    Returns list of variants ordered longest-first (so longer/more-specific
    matches win)."""
    if not product_name: return []
    n = product_name.lower()
    # Drop punctuation except letters/digits/spaces/underscores/+
    n = re.sub(r'[^a-z0-9 _+]', ' ', n)
    n = re.sub(r'\s+', ' ', n).strip()
    if not n: return []
    # Common product noise suffixes
    n = re.sub(r'\s+(combo|pack|set|kit|bundle|special|edition).*$', '', n)
    n = n.strip()
    if not n: return []
    variants = set()
    underscored = n.replace(' ', '_').strip('_')
    if underscored: variants.add(underscored)
    # Collapsed (no spaces)
    collapsed = n.replace(' ', '')
    if collapsed and collapsed != underscored: variants.add(collapsed)
    # Filter: must be long enough + not on blacklist + not just digits
    out = [v for v in variants
           if len(v) >= MIN_KEYWORD_LEN
           and v not in GENERIC_TOKEN_BLACKLIST
           and not v.isdigit()]
    out.sort(key=lambda x: -len(x))   # longest first
    return out


def build_product_keyword_index(ntn_to_product: dict) -> list:
    """Returns list of (keyword, ntn_code) sorted by keyword length DESC.
    Longer keywords win first → 'richie_rich_combo' beats 'richie_rich'."""
    idx = {}     # keyword → ntn_code (first writer wins)
    for ntn_code, product in ntn_to_product.items():
        if not product: continue
        for kw in _normalize_product_to_keywords(product):
            if kw not in idx:
                idx[kw] = ntn_code
    return sorted(idx.items(), key=lambda kv: -len(kv[0]))


def match_product_from_name(text: str, keyword_index: list) -> str | None:
    """Scan text for any product keyword. Returns NTN code on first match
    (which is also the longest because keyword_index is sorted desc)."""
    if not text: return None
    t = text.lower()
    for kw, ntn in keyword_index:
        # Token-aware boundary check (same pattern as creative_type)
        if re.search(rf'(?:^|_|\s){re.escape(kw)}(?:$|_|\s)', t):
            return ntn
    return None

# ── Regex patterns ────────────────────────────────────────────────────────
# NTN code: matches NTN1234, ntn1234 (case-insensitive). Word boundary on both
# sides prevents matching mid-word. We allow both 'ntn222' and 'ntn_222' just
# in case nomenclature drifts.
NTN_PATTERN = re.compile(r'(?<![A-Za-z0-9])(NTN_?(\d{2,5}))(?![A-Za-z0-9])', re.IGNORECASE)

# Sentiment: _st1_, _st12_, _st101a_, _st102f_, etc. Anchored to
# underscores to avoid matching things like "static" (which has 'st' but
# not as a code). Captures optional A-F letter suffix for ST10X family
# (permutation indicator — Quality+Achievement+Testing has 6 orderings).
SENTIMENT_PATTERN = re.compile(
    r'(?<![A-Za-z0-9])(st(\d{1,3})([a-f])?)(?![A-Za-z0-9])',
    re.IGNORECASE,
)

# Only these sentiment codes are considered valid. Anything outside this
# set (e.g. _st9_, _st113a_) is treated as unset — keeps the Sentiments
# page limited to the operator's defined taxonomy. Single source of
# truth lives in seed_lookups.SENTIMENT_SEED; we mirror just the codes
# here so classify_ads doesn't pull in gspread / sqlite at import time.
ALLOWED_SENTIMENTS = (
    # ST1, ST2, ST4 — six A-F permutations each; ST3 — two (a/b).
    # (Split from the legacy un-lettered st1..st4 per sentiments.xlsx, 06-04.)
    {f'st{n}{s}' for n in (1, 2, 4) for s in 'abcdef'} |
    {'st3a', 'st3b'} |
    # ST101-107, 109 — six A-F permutations each
    {f'st{n}{s}'
     for n in (101, 102, 103, 104, 105, 106, 107, 109)
     for s in 'abcdef'} |
    # ST108, 110, 111, 112 — single A
    {'st108a', 'st110a', 'st111a', 'st112a'}
)

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
    """Returns a sentiment code like 'st1a' / 'st101a' / 'st108a'
    (lowercased) or None.

    Two-pass matcher:
      1. Explicit code tag in the name (`_st101a_`, `_st1_`, etc.).
      2. Fallback: theme-keyword match on the ad name. Each sentiment
         family has a few signature themes (e.g. ST101 → quality /
         achievement / testing). If at least 2 of a family's themes
         appear in the name, classify to that family's canonical 'A'
         variant. Lets ads named with the theme words instead of the
         code (e.g. 'style_design_quality_crystal_loose') still get
         picked up.

    Codes outside ALLOWED_SENTIMENTS are ignored — the dashboard's
    Sentiments page only tracks the operator's defined taxonomy."""
    if not text: return None

    # Pass 1: explicit code tag
    m = SENTIMENT_PATTERN.search(text)
    if m:
        digits = m.group(2)
        suffix = (m.group(3) or '').lower()
        # Storyline codes st1..st4 were split into A-F variants (06-04).
        # Bare 'st1' has no ordering letter → default to canonical 'a'.
        if not suffix and digits in ('1', '2', '3', '4'):
            suffix = 'a'
        code = f'st{digits}{suffix}'
        if code in ALLOWED_SENTIMENTS:
            return code

    # Pass 2: theme-keyword match (operator names ads with theme words,
    # not the st<n> code). Family → list of theme tokens. Order is
    # significant only inside an A-F family for variant disambiguation,
    # but here we just need ≥2 themes present to pick the family's 'A'
    # variant. Tokens use _has_token (underscore-boundary), so 'crystal'
    # matches '_crystal_' but not 'crystalize'.
    t = (text or '').lower()
    SENTIMENT_THEMES = [
        # (family_letter_code, [theme_tokens])
        ('st1a',   ['style', 'design', 'crystal']),         # Style+Design+Quality+Crystal Energy
        ('st2a',   ['unisex', 'crystal']),                  # Unisex+Quality+Crystal Energy (unisex unique enough)
        ('st3a',   ['og_gold', 'gold_price', 'price_fear']),# OG Gold Price Fear
        ('st4a',   ['animal', 'horse', 'owl', 'peacock', 'cheetah', 'elephant', 'butterfly']),  # Animal Storyline
        ('st101a', ['achievement', 'testing', 'tested', 'certified']),
        ('st102a', ['fear', 'problem', 'solution', 'validation']),
        ('st103a', ['desire', 'fast_results', 'fast', 'results']),
        ('st104a', ['before', 'after', 'before_after', 'testimonial']),
        ('st105a', ['relevance', 'validation', 'trust']),
        ('st106a', ['ingredient', 'benefits']),
        ('st107a', ['manufacturing', 'rnd', 'lab', 'r_d']),
        ('st108a', ['offer', 'deal', 'discount', 'sale']),
        ('st109a', ['competitor', 'comparison', 'vs_other']),
        ('st110a', ['celebrity', 'social_proof', 'celeb']),
        ('st111a', ['daily_routine', 'routine', 'morning']),
        ('st112a', ['launch', 'bts', 'behind_scenes', 'usage']),
    ]
    best_code = None
    best_hits = 0
    for code, themes in SENTIMENT_THEMES:
        if code not in ALLOWED_SENTIMENTS:
            continue
        hits = sum(1 for kw in themes if _has_token(t, kw))
        # ≥2 themes for compound families; ≥1 for distinctive singletons
        threshold = 1 if code in ('st108a', 'st110a', 'st111a', 'st112a',
                                   'st3a') else 2
        if hits >= threshold and hits > best_hits:
            best_code = code
            best_hits = hits
    return best_code


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


# Tokens that are noise in campaign names — strip these before deriving a
# product slug. Includes: account-prefix words, funnel/intent words,
# creative-type words (handled separately), audience tokens, dates, status
# markers. Tuned conservatively — only common SM/SML/NBP boilerplate.
_SLUG_NOISE = {
    'ntn', 'adv', 'web', 'app', 'sales', 'sale', 'conv', 'rtg', 'retarget',
    'loose', 'visitor', 'impression', 'master', 'mix', 'camp', 'campaign',
    'reel', 'video', 'static', 'image', 'post', 'carousel', 'creative',
    'tof', 'mof', 'bof', 'clp', 'lp', 'offer', 'sales1', 'test', 'testing',
    'copy', 'ds', 'paras', 'wanda', 'partnership', 'collab', 'creator',
    'motion', 'brand', 'inde', 'india', 'insta', 'ig', 'fb', 'meta',
    'desire', 'launches', 'latest', 'fresh', 'new', 'old', 'app',
    # category words — handled by category column already
    'skin', 'hair', 'crystal', 'crystals', 'perfumes', 'perfume',
    'nutra', 'nutraceuticals', 'aibot', 'jewellery_set',
    # Audience segments (we have audiences in the adset table now)
    'seg1', 'seg2', 'seg3', 'seg4', 'seg5', 'seg6', 'seg7', 'seg8',
    'seg9', 'seg10', 'seg11', 'seg12', 'seg13', 'seg14', 'seg15',
    'lla', '180dp', '180imp', 'exc30', '60dp', '7d',
    'r', 's',  # single-letter / very short
}
_DATE_TOKEN_RE = re.compile(r'^\d{4,8}$')   # eg "090526", "20250526"


def extract_product_slug(campaign_name: str) -> str | None:
    """Heuristic product key derived from the campaign name when neither
    NTN-code extraction nor SKU keyword match worked. Strips funnel/audience/
    creative-type/date noise; what remains is usually the product name in
    the operator's short form (e.g. '24k_gold_serum', 'jewellery',
    'ampmcombo', 'peacock_frame'). Returns None if nothing useful is left.
    """
    if not campaign_name: return None
    t = campaign_name.lower()
    # Normalise separators
    t = re.sub(r'[\-/]', '_', t)
    t = re.sub(r'[^a-z0-9_]+', '_', t)
    tokens = [tok for tok in t.split('_') if tok]
    keep = []
    for tok in tokens:
        if tok in _SLUG_NOISE: continue
        if _DATE_TOKEN_RE.match(tok): continue
        if len(tok) <= 1: continue
        keep.append(tok)
    if not keep: return None
    # Cap to a manageable length so we don't produce hundreds of unique
    # slugs that differ only by trailing tokens.
    return '~' + '_'.join(keep[:4])   # '~' prefix marks auto-derived


def classify_one(ad_name: str, campaign_name: str,
                 ntn_to_category: dict = None,
                 product_keyword_index: list = None) -> dict:
    """Returns dict of all derived fields for one ad.
    Path B (v4+): if no NTN code from regex, fall back to product name
    matching against the SKU sheet keyword index.
    Path C: if both fail, derive a product slug from the campaign name so
    the operator's filter dropdown still surfaces this ad's grouping."""
    ntn_to_category = ntn_to_category or {}
    product_keyword_index = product_keyword_index or []
    combined = f"{ad_name or ''} {campaign_name or ''}"
    # 1. Regex-extract NTN code (most reliable when present)
    ntn_code = extract_ntn_code(combined)
    # 2. Path B: if no NTN code, scan for product name keywords in ad text
    matched_via_name = False
    if not ntn_code and product_keyword_index:
        ntn_code = match_product_from_name(combined, product_keyword_index)
        if ntn_code: matched_via_name = True
    # 3. Path C: if still no match, derive a slug from the campaign name.
    # This won't map to an NTN code but populates a "product" label so the
    # ad shows up in the filter dropdown.
    derived_slug = None
    if not ntn_code:
        derived_slug = extract_product_slug(campaign_name)
    return {
        'category':       derive_category_with_ntn(ad_name, campaign_name,
                                                   ntn_code, ntn_to_category),
        'creative_type':  extract_creative_type(ad_name, campaign_name),
        'sentiment':      extract_sentiment(combined),
        'ntn_code':       ntn_code,
        'derived_product_slug': derived_slug,
        'matched_via_name': matched_via_name,   # diagnostic only
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
    # Path B: build product-keyword index for ad-name fallback matching
    product_keyword_index = build_product_keyword_index(ntn_to_product)
    print(f"  Built product keyword index: {len(product_keyword_index)} keywords")

    print(f"Classifying {len(ads)} ad(s)...")
    rows = []
    counts = {
        'category': {}, 'creative_type': {}, 'sentiment_set': 0,
        'ntn_set': 0, 'product_set': 0, 'matched_via_name': 0,
    }
    for ad_id, ad_name, campaign_id in ads:
        cname = camp_names.get(campaign_id, '')
        cls = classify_one(ad_name, cname,
                           ntn_to_category=ntn_to_category,
                           product_keyword_index=product_keyword_index)
        product = ntn_to_product.get(cls['ntn_code']) if cls['ntn_code'] else None
        # Path C fallback: use the campaign-name slug so this ad still gets
        # a grouping label that surfaces in the dashboard's product filter.
        if not product and cls.get('derived_product_slug'):
            product = cls['derived_product_slug']
        if cls.get('matched_via_name'):
            counts['matched_via_name'] += 1

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
    print(f"     · via regex:     {counts['ntn_set'] - counts['matched_via_name']}")
    print(f"     · via name match (Path B): {counts['matched_via_name']}")
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
