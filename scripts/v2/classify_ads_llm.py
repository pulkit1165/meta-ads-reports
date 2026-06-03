#!/usr/bin/env python3
"""
Classify ad creatives into the 4 sentiment buckets (st1-st4) using Claude API.

Pulls ads from meta_ads_meta where sentiment IS NULL AND total_spend > 100,
fetches body+title+message text from Meta Graph API, batches 20 per Claude
call (with prompt caching on the sentiment-definition system prompt), and
writes back the assigned code (or leaves NULL if none of the 4 fit).

Idempotent — only operates on rows where sentiment IS NULL, so re-runs
pick up newly-ingested ads without re-classifying anything already tagged.

Usage:
  python3 scripts/v2/classify_ads_llm.py [--limit 500] [--dry-run]
"""

import os, sys, json, time, argparse, requests
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _utils import db_connect, now_iso  # noqa: E402

from dotenv import load_dotenv  # noqa: E402
import anthropic  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
load_dotenv(_REPO_ROOT / '.env')

META_TOKEN = os.getenv('META_ACCESS_TOKEN')
ANTHROPIC_KEY = os.getenv('ANTHROPIC_API_KEY')
GRAPH = 'https://graph.facebook.com/v19.0'
IST = ZoneInfo('Asia/Kolkata')

# Keep in sync with SENTIMENT_SEED in seed_lookups.py and ALLOWED_SENTIMENTS
# in classify_ads.py. 4 legacy codes + 52 storyline-framework codes.
# Single source of truth: seed_lookups.SENTIMENT_SEED — we pull from there
# at import so adding a code in one place suffices.
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent))
from seed_lookups import SENTIMENT_SEED  # noqa: E402

# Short per-code descriptions to help the LLM disambiguate. Anything not
# listed falls back to just the label from SENTIMENT_SEED.
_SENTIMENT_DESCRIPTIONS = {
    'st1': 'Crystal product. Emphasizes aesthetics / design / craft quality. "Design", "premium look", "stylish", "elegant".',
    'st2': 'Crystal product positioned as unisex / for everyone. "Unisex", "for him and her", "any gender".',
    'st3': 'Gold price increases or fear-of-missing-out around gold value as the hook. "Gold price rising", "before prices go up", "OG gold".',
    'st4': 'Paras (founder/face) appears in the storyline alongside crystal energy + quality framing.',
    'st108a': 'Pure offer / deal hook — discount, free gift, limited time.',
    'st110a': 'Celebrity endorsement or strong social proof (followers, awards) as the lead.',
    'st111a': 'Daily routine / habit / lifestyle integration framing.',
    'st112a': 'New launch — purpose + behind-the-scenes + usage demo.',
}
# Family-level descriptions for ST101-107, ST109 (permutations differ only
# by ordering, so the LLM picks the variant that matches what the ad opens
# with → builds to → closes with).
_FAMILY_DESCRIPTIONS = {
    'st101': 'Quality, Achievement (awards/sales), and Testing (lab/cert) in some order.',
    'st102': 'Fear-Based Problem, SKU as Solution, Validation in some order.',
    'st103': 'Desire, Fast Results (timeframe promised), Quality in some order.',
    'st104': 'Before/After transformation, Testimonial, Testing in some order.',
    'st105': 'Relevance (specific persona), Validation, Trust in some order.',
    'st106': 'Ingredient Story, Benefits, Quality in some order.',
    'st107': 'Manufacturing/R&D, Quality, Achievements in some order.',
    'st109': 'Competitor Comparison, Achievements, Trust in some order.',
}

SENTIMENT_DEFS = []
for code, label in SENTIMENT_SEED:
    desc = _SENTIMENT_DESCRIPTIONS.get(code)
    if not desc:
        # ST10X family — give the family hint + the specific ordering
        fam_key = code[:5]  # 'st101', 'st102', etc.
        fam_hint = _FAMILY_DESCRIPTIONS.get(fam_key, '')
        desc = (
            f'Theme: {label}. ' + (fam_hint + ' ' if fam_hint else '') +
            'Pick this letter variant when the ad presents the elements in '
            'exactly this order (open → middle → close).'
        )
    SENTIMENT_DEFS.append((code, label, desc))

SYSTEM_PROMPT = """You classify ad creatives into one of 4 sentiment codes,
or return null if none of the 4 fit. Output JSON only.

Sentiment codes:
""" + '\n'.join(
    f'- {code}: {label}\n  {desc}'
    for code, label, desc in SENTIMENT_DEFS
) + """

Rules:
- If the ad clearly fits exactly one code, return that code.
- If the ad is about Skin, Hair, Perfume, Nutraceuticals, or anything not
  in the 4 codes above, return null.
- If multiple codes could fit, choose the dominant theme. Animal storyline
  beats general crystal energy (st4 > st1). Gold-price fear beats general
  style (st3 > st1). Unisex framing beats general style (st2 > st1).
- Be strict — when in doubt, return null. Wrong codes are worse than nulls.
"""

BATCH_SIZE = 20
MODEL = 'claude-sonnet-4-6'

# JSON schema enforced via output_config.format — no prefills, no brittle parsing.
OUTPUT_SCHEMA = {
    'type': 'object',
    'properties': {
        'classifications': {
            'type': 'array',
            'items': {
                'type': 'object',
                'properties': {
                    'ad_id':     {'type': 'string'},
                    'sentiment': {'type': ['string', 'null'],
                                  'enum': [code for code, _ in SENTIMENT_SEED] + [None]},
                },
                'required': ['ad_id', 'sentiment'],
                'additionalProperties': False,
            },
        },
    },
    'required': ['classifications'],
    'additionalProperties': False,
}


# ── Meta creative fetch ───────────────────────────────────────────────────────

def fetch_with_backoff(url, params, attempts=6):
    for i in range(attempts):
        try:
            r = requests.get(url, params=params, timeout=30)
            data = r.json()
        except Exception as e:
            print(f"    network error: {e}, retry in {2**i}s", file=sys.stderr)
            time.sleep(2 ** i)
            continue
        if 'error' in data and 'limit' in data['error'].get('message', '').lower():
            wait = 30 * (2 ** i)
            print(f"    rate-limited, sleep {wait}s", file=sys.stderr)
            time.sleep(wait)
            continue
        return data
    return None


def fetch_creative_text(ad_id):
    """Pull body + title + spec.message + spec.name from Meta. Empty string on failure."""
    data = fetch_with_backoff(
        f"{GRAPH}/{ad_id}",
        {
            'access_token': META_TOKEN,
            'fields': 'creative{body,title,object_story_spec,name}',
        },
    )
    if not data or 'creative' not in data:
        return ''
    c = data['creative']
    parts = [c.get('body', '') or '', c.get('title', '') or '', c.get('name', '') or '']
    spec = c.get('object_story_spec') or {}
    for k in ('link_data', 'video_data', 'photo_data'):
        d = spec.get(k) or {}
        parts.extend([d.get('message', '') or '', d.get('name', '') or '',
                      d.get('description', '') or '', d.get('caption', '') or ''])
    return ' '.join(p for p in parts if p).strip()


# ── Claude classification ─────────────────────────────────────────────────────

def classify_batch(client, batch):
    """batch: list of dicts {ad_id, ad_name, creative_text}.
    Returns {ad_id: 'st1'|'st2'|'st3'|'st4'|None}."""
    lines = []
    for a in batch:
        # Trim creative text to keep prompt reasonable. Names + first ~600
        # chars of body is usually enough to classify.
        ctext = (a.get('creative_text') or '')[:600]
        lines.append(
            f"--- {a['ad_id']} ---\n"
            f"ad_name: {a.get('ad_name') or '(no name)'}\n"
            f"creative: {ctext or '(no creative text fetched)'}\n"
        )
    user_msg = (
        "Classify each ad below into st1, st2, st3, st4, or null.\n"
        "Return JSON matching the schema.\n\n"
        + '\n'.join(lines)
    )

    resp = client.messages.create(
        model=MODEL,
        max_tokens=4000,
        system=[{
            'type': 'text',
            'text': SYSTEM_PROMPT,
            'cache_control': {'type': 'ephemeral'},
        }],
        output_config={
            'format': {'type': 'json_schema', 'schema': OUTPUT_SCHEMA},
        },
        messages=[{'role': 'user', 'content': user_msg}],
    )

    text = next((b.text for b in resp.content if b.type == 'text'), '')
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as e:
        print(f"    JSON parse error: {e}; raw: {text[:200]}", file=sys.stderr)
        return {}

    out = {}
    for item in parsed.get('classifications', []):
        aid = item.get('ad_id')
        sent = item.get('sentiment')
        if not aid:
            continue
        if sent in {c for c, _ in SENTIMENT_SEED}:
            out[str(aid)] = sent
        else:
            out[str(aid)] = None

    # Log cache effectiveness so the operator can verify caching is working.
    u = resp.usage
    cache_read = getattr(u, 'cache_read_input_tokens', 0) or 0
    cache_write = getattr(u, 'cache_creation_input_tokens', 0) or 0
    print(f"    Claude: in={u.input_tokens} cache_read={cache_read} "
          f"cache_write={cache_write} out={u.output_tokens}", file=sys.stderr)

    return out


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--limit', type=int, default=500,
                    help='Max ads to classify this run (default 500)')
    ap.add_argument('--dry-run', action='store_true',
                    help='Classify but do not UPDATE the DB')
    args = ap.parse_args()

    if not META_TOKEN:
        sys.exit('ERROR: META_ACCESS_TOKEN not set in .env')
    if not ANTHROPIC_KEY:
        sys.exit('ERROR: ANTHROPIC_API_KEY not set in .env')

    conn = db_connect()
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

    started_at = datetime.now(IST).isoformat()
    print(f"🤖 classify_ads_llm — {started_at}")
    print(f"   model={MODEL}  batch_size={BATCH_SIZE}  limit={args.limit}  "
          f"dry_run={args.dry_run}")

    rows = conn.execute(
        '''SELECT ad_id, ad_name
           FROM meta_ads_meta
           WHERE sentiment IS NULL AND total_spend > 100
           ORDER BY total_spend DESC
           LIMIT ?''',
        (args.limit,)
    ).fetchall()

    if not rows:
        print("   No NULL-sentiment ads with spend > 100. Nothing to do.")
        return

    print(f"   Found {len(rows)} ads to classify")

    ads = []
    for i, (ad_id, ad_name) in enumerate(rows, 1):
        text = fetch_creative_text(ad_id)
        ads.append({'ad_id': ad_id, 'ad_name': ad_name or '', 'creative_text': text})
        if i % 50 == 0:
            print(f"   fetched creative for {i}/{len(rows)}")
        time.sleep(0.15)  # gentle on Meta rate limits
    print(f"   fetched creative for {len(ads)}/{len(rows)}")

    counts = {code: 0 for code, _ in SENTIMENT_SEED}
    counts['null'] = 0
    counts['missing'] = 0
    total_batches = (len(ads) + BATCH_SIZE - 1) // BATCH_SIZE

    for bi in range(0, len(ads), BATCH_SIZE):
        batch = ads[bi:bi + BATCH_SIZE]
        bnum = (bi // BATCH_SIZE) + 1
        print(f"  Batch {bnum}/{total_batches} ({len(batch)} ads)...")
        try:
            results = classify_batch(client, batch)
        except anthropic.RateLimitError as e:
            wait = int(e.response.headers.get('retry-after', '30')) if e.response else 30
            print(f"    Claude rate-limited, sleep {wait}s, retrying", file=sys.stderr)
            time.sleep(wait)
            try:
                results = classify_batch(client, batch)
            except Exception as e2:
                print(f"    retry failed: {e2}", file=sys.stderr)
                results = {}
        except anthropic.APIStatusError as e:
            print(f"    Claude API error {e.status_code}: {e.message}", file=sys.stderr)
            results = {}

        for a in batch:
            aid = a['ad_id']
            if aid not in results:
                counts['missing'] += 1
                continue
            sent = results[aid]
            if sent is None:
                counts['null'] += 1
                continue
            counts[sent] += 1
            if not args.dry_run:
                # meta_ads_meta has no updated_at column (per db_schema.sql)
                conn.execute(
                    'UPDATE meta_ads_meta SET sentiment = ? WHERE ad_id = ?',
                    (sent, aid),
                )

    print()
    print(f"Classification done — {datetime.now(IST).isoformat()}")
    # Print non-zero buckets only — 56 codes is a lot of noise otherwise.
    for code, label in SENTIMENT_SEED:
        if counts.get(code, 0):
            print(f"  {code:8s} {label[:40]:40s} {counts[code]}")
    print(f"  {'null':8s} {'(no fit)':40s} {counts['null']}")
    print(f"  {'missing':8s} {'(no LLM result)':40s} {counts['missing']}")
    if args.dry_run:
        print("  (dry-run — DB not updated)")


if __name__ == '__main__':
    main()
