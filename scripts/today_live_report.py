#!/usr/bin/env python3
"""
Today's Live Report — multi-section snapshot tab refreshed throughout the day.

Tab: 🔴 Today's Live DD MMM YY  (delete + recreate every run, clean state)

Sections:
  1. By Type → Audience    Prospecting / Retarget / TOF blocks with per-audience
                           rows (budget, spend, ROAS, # camps).
  2. Daily Success Rate    Spend in ROAS buckets ≥1.0 / ≥1.5 / ≥1.75 / ≥2.0 / ≥3.0,
                           plus "Below 1x" so 100% is preserved.
  3. Best Creatives        Top ad-level performers + % spend split by creative
                           type (Paras / Partnership / Static / Motion / Catalogue
                           / Testing / Others).

Cadence: every 2 hours during business hours IST (managed by today-live.yml).
"""

import os, sys, json, argparse, requests, time
from datetime import datetime, timedelta
from collections import defaultdict
from pathlib import Path
from zoneinfo import ZoneInfo

import gspread
from google.oauth2.service_account import Credentials
from dotenv import load_dotenv

# ── Path setup ────────────────────────────────────────────────────────────────
_REPO_ROOT = Path(__file__).resolve().parent.parent
STATE_DIR  = Path(os.environ.get('META_REPORTS_STATE_DIR') or (_REPO_ROOT / 'state'))
OUT_DIR    = Path(os.environ.get('META_REPORTS_OUT_DIR')   or (_REPO_ROOT / 'out'))
STATE_DIR.mkdir(parents=True, exist_ok=True)
OUT_DIR.mkdir(parents=True, exist_ok=True)
load_dotenv(_REPO_ROOT / '.env')

# Reuse type/segment derivation
sys.path.insert(0, str(_REPO_ROOT / 'scripts'))
from product_catalogue import derive_type, derive_segment, derive_category_only

TOKEN    = os.getenv('META_ACCESS_TOKEN')
SA_FILE  = os.environ.get('GOOGLE_SERVICE_ACCOUNT_FILE') or str(_REPO_ROOT / 'google-service-account.json')
SHEET_ID = os.environ.get('REPORTS_SHEET_ID') or '1hJ3IS2VDtTAEyyJIV__jvts9CMQdYhyxKAfWKtrkUH4'
SCOPES   = ['https://www.googleapis.com/auth/spreadsheets']
GRAPH    = 'https://graph.facebook.com/v19.0'
IST      = ZoneInfo('Asia/Kolkata')

# SM-only per master doc (NBP is Madhav Ji's territory, DS excluded)
ACCOUNTS = [
    (os.getenv('SM_FRAGRANCE_01'),   'SM Fragrance 01'),
    (os.getenv('SM_SKIN'),           'SM Skin'),
    (os.getenv('SM_HAIR'),           'SM Hair'),
    (os.getenv('SM_CRYSTALS'),       'SM Crystals'),
    (os.getenv('SM_PERFUME'),        'SM Perfume'),
    (os.getenv('SM_CREDIT_LINE_05'), 'SM CL 05'),
    (os.getenv('SM_CREDIT_LINE_06'), 'SM CL 06'),
]

# ROAS buckets for the success-rate section (ascending)
ROAS_BUCKETS = [1.0, 1.5, 1.75, 2.0, 3.0]


# ── Helpers ───────────────────────────────────────────────────────────────────
def safe_float(x):
    try: return float(x or 0)
    except (TypeError, ValueError): return 0.0


def extract_roas(raw, prefer='value'):
    """purchase_roas → float. Uses 'value' (default attribution) to match Ads Manager."""
    if not raw: return 0.0
    if isinstance(raw, list) and raw:
        item = raw[0]
        if isinstance(item, dict):
            v = item.get(prefer) or item.get('value', 0)
            return safe_float(v)
    return 0.0


def derive_creative_type(ad_name):
    """Match build_creative_dashboard.py classification."""
    n = (ad_name or '').lower()
    if 'paras' in n: return 'Paras'
    if any(k in n for k in ('partnership', 'collab', 'influencer', 'aliabhat')): return 'Partnership'
    if any(k in n for k in ('catalog', 'dpa', 'carousel')): return 'Catalogue'
    if any(k in n for k in ('static', 'image', 'banner')): return 'Static'
    if any(k in n for k in ('reel', 'video', 'motion', 'inde')): return 'Motion'
    if n.startswith('testing_') or n.startswith('test_'): return 'Testing'
    return 'Others'


def paginate(url, params, max_pages=50):
    rows, page = [], 0
    params = {**params, 'access_token': TOKEN}
    while page < max_pages:
        r = requests.get(url, params=params, timeout=30).json()
        if 'error' in r:
            err = r['error']
            if err.get('code') == 17:  # rate limit
                time.sleep(60); continue
            print(f"  ⚠️  {err.get('message','API error')[:100]}", file=sys.stderr)
            return rows
        rows.extend(r.get('data', []))
        nxt = r.get('paging', {}).get('next')
        if not nxt: break
        url, params = nxt, {}
        page += 1
    return rows


# ── Fetch ─────────────────────────────────────────────────────────────────────
def fetch_today_campaigns(date_str):
    """Returns list of camp dicts for SM with spend>0 today."""
    out = []
    for acct, acct_name in ACCOUNTS:
        if not acct:
            continue
        # Live insights: today only, default attribution
        ins = paginate(f"{GRAPH}/{acct}/insights", {
            'level': 'campaign',
            'fields': 'campaign_id,campaign_name,spend,impressions,clicks,purchase_roas',
            'time_range': json.dumps({'since': date_str, 'until': date_str}),
            'filtering': json.dumps([{'field': 'spend', 'operator': 'GREATER_THAN', 'value': '0'}]),
            'limit': 500,
        })
        if not ins:
            continue
        # Campaign list: need daily_budget + adsets to compute true budget
        camp_meta = {c['id']: c for c in paginate(f"{GRAPH}/{acct}/campaigns", {
            'fields': 'id,name,status,daily_budget,adsets{daily_budget,status}',
            'effective_status': '["ACTIVE"]',
            'limit': 500,
        })}
        for r in ins:
            cid = r['campaign_id']
            meta = camp_meta.get(cid, {})
            cbo = safe_float(meta.get('daily_budget'))
            if cbo > 0:
                budget = cbo / 100
            else:
                adsets = (meta.get('adsets') or {}).get('data') or []
                budget = sum(safe_float(a.get('daily_budget')) / 100
                             for a in adsets if a.get('status') == 'ACTIVE')
            name = r.get('campaign_name', '')
            out.append({
                'cid':      cid,
                'account':  acct_name,
                'name':     name,
                'spend':    safe_float(r.get('spend')),
                'roas':     extract_roas(r.get('purchase_roas')),
                'budget':   budget,
                'type':     derive_type(name),
                'segment':  derive_segment(name),
                'category': derive_category_only(name),
            })
    return out


def fetch_today_ads(date_str):
    """Ad-level today insights for the creative section."""
    out = []
    for acct, acct_name in ACCOUNTS:
        if not acct:
            continue
        rows = paginate(f"{GRAPH}/{acct}/insights", {
            'level': 'ad',
            'fields': 'ad_id,ad_name,campaign_name,spend,impressions,clicks,purchase_roas',
            'time_range': json.dumps({'since': date_str, 'until': date_str}),
            'filtering': json.dumps([{'field': 'spend', 'operator': 'GREATER_THAN', 'value': '0'}]),
            'limit': 500,
        })
        for r in rows:
            name = r.get('ad_name', '')
            out.append({
                'aid':           r.get('ad_id'),
                'account':       acct_name,
                'name':          name,
                'campaign':      r.get('campaign_name', ''),
                'spend':         safe_float(r.get('spend')),
                'roas':          extract_roas(r.get('purchase_roas')),
                'creative_type': derive_creative_type(name),
            })
    return out


# ── Sections ──────────────────────────────────────────────────────────────────
def fmt_inr(n):  return f"₹{int(n):,}"
def fmt_pct(p):  return f"{p:.1f}%"
def fmt_roas(r):
    if not r:    return '—'
    return f"{r:.2f}x"


def section_audience_breakdown(camps, ts_label):
    rows = []
    rows.append([f"📊  LIVE TRACKER BY TYPE × AUDIENCE  |  Updated {ts_label}",
                 '', '', '', '', ''])

    # Map our internal type names → user-facing block headers
    BLOCKS = [
        ('Sales',    'PROSPECTING'),
        ('Retarget', 'RETARGET'),
    ]
    # 'TOF' is handled implicitly — derive_type only returns 'Sales' / 'Retarget'
    # from product_catalogue, so we don't render a separate TOF block here.

    for type_key, header in BLOCKS:
        block_camps = [c for c in camps if c['type'] == type_key]
        if not block_camps:
            continue

        # Aggregate by segment within this block
        agg = defaultdict(lambda: {'count': 0, 'spend': 0.0, 'budget': 0.0, 'rev': 0.0})
        for c in block_camps:
            seg = c['segment'] or 'Unmapped'
            a = agg[seg]
            a['count']  += 1
            a['spend']  += c['spend']
            a['budget'] += c['budget']
            a['rev']    += c['spend'] * c['roas']

        # Block totals
        t_camps  = sum(a['count']  for a in agg.values())
        t_spend  = sum(a['spend']  for a in agg.values())
        t_budget = sum(a['budget'] for a in agg.values())
        t_rev    = sum(a['rev']    for a in agg.values())
        t_roas   = (t_rev / t_spend) if t_spend else 0.0

        rows.append([f"━━ {header} ━━  {t_camps} camps  |  Budget {fmt_inr(t_budget)}  |  Spent {fmt_inr(t_spend)}  |  ROAS {fmt_roas(t_roas)}",
                     '', '', '', '', ''])
        rows.append(['Audience', '# Camps', 'Budget (₹)', 'Spent (₹)', '% of Block Spend', 'ROAS'])

        # Order audiences by spend desc
        for seg, a in sorted(agg.items(), key=lambda kv: -kv[1]['spend']):
            roas = (a['rev'] / a['spend']) if a['spend'] else 0.0
            share = (a['spend'] / t_spend * 100) if t_spend else 0.0
            rows.append([
                seg,
                a['count'],
                int(a['budget']),
                int(a['spend']),
                fmt_pct(share),
                fmt_roas(roas),
            ])
        rows.append([''])
    return rows


def section_success_rate(camps, ts_label):
    rows = []
    rows.append([f"🎯  DAILY SUCCESS RATE  |  Spend by ROAS bucket  |  {ts_label}",
                 '', '', '', '', ''])

    total_spend = sum(c['spend'] for c in camps)
    total_camps = len(camps)

    if total_spend == 0:
        rows.append(['No spend yet today', '', '', '', '', ''])
        rows.append([''])
        return rows

    rows.append(['Bucket', 'Threshold', '# Camps', 'Spend (₹)', '% of Total Spend', 'Notes'])

    # Each bucket: spend on camps with roas >= threshold (cumulative — not exclusive ranges)
    # Plus a "Below 1x" complementary bucket.
    below = [c for c in camps if c['roas'] < 1.0]
    rows.append([
        '🔴 Below 1.0x',
        '< 1.0x',
        len(below),
        int(sum(c['spend'] for c in below)),
        fmt_pct(sum(c['spend'] for c in below) / total_spend * 100),
        'Lost / on close watch',
    ])

    bucket_emojis = {1.0: '🟡', 1.5: '🟢', 1.75: '🟢', 2.0: '🟢', 3.0: '⭐'}
    for threshold in ROAS_BUCKETS:
        matching = [c for c in camps if c['roas'] >= threshold]
        spend = sum(c['spend'] for c in matching)
        rows.append([
            f"{bucket_emojis.get(threshold,'🟢')} ≥ {threshold}x",
            f"≥ {threshold}x",
            len(matching),
            int(spend),
            fmt_pct(spend / total_spend * 100),
            '',
        ])

    rows.append(['TOTAL TODAY', '', total_camps, int(total_spend), '100%', ''])
    rows.append([''])
    return rows


def section_best_creatives(ads, ts_label, top_n=10, min_spend=500):
    rows = []
    rows.append([f"🎨  BEST CREATIVES TODAY  |  Ad-level  |  Top {top_n} by ROAS (min ₹{min_spend} spend)  |  {ts_label}",
                 '', '', '', '', ''])

    if not ads:
        rows.append(['No ad-level data yet today', '', '', '', '', ''])
        rows.append([''])
        return rows

    # Top performers
    eligible = [a for a in ads if a['spend'] >= min_spend]
    eligible.sort(key=lambda x: -x['roas'])

    rows.append(['Rank', 'Ad Name', 'Creative Type', 'Account', 'Spend (₹)', 'ROAS'])
    for i, a in enumerate(eligible[:top_n], 1):
        rows.append([
            i,
            a['name'][:80],
            a['creative_type'],
            a['account'],
            int(a['spend']),
            fmt_roas(a['roas']),
        ])
    rows.append([''])

    # Creative type % split
    rows.append([f"📐  CREATIVE TYPE MIX  |  All running ads (any spend)",
                 '', '', '', '', ''])
    rows.append(['Creative Type', '# Ads', 'Spend (₹)', '% of Spend', 'Avg ROAS', ''])

    type_agg = defaultdict(lambda: {'count': 0, 'spend': 0.0, 'rev': 0.0})
    for a in ads:
        ta = type_agg[a['creative_type']]
        ta['count'] += 1
        ta['spend'] += a['spend']
        ta['rev']   += a['spend'] * a['roas']

    total = sum(t['spend'] for t in type_agg.values())
    order = ['Paras', 'Partnership', 'Motion', 'Static', 'Catalogue', 'Testing', 'Others']
    for ct in order:
        if ct not in type_agg:
            continue
        t = type_agg[ct]
        roas = (t['rev'] / t['spend']) if t['spend'] else 0.0
        share = (t['spend'] / total * 100) if total else 0.0
        rows.append([
            ct,
            t['count'],
            int(t['spend']),
            fmt_pct(share),
            fmt_roas(roas),
            '',
        ])
    rows.append([''])
    return rows


# ── Sheet write ───────────────────────────────────────────────────────────────
def date_label(date_str):
    return datetime.strptime(date_str, '%Y-%m-%d').strftime('%d %b %y').upper()


def write(rows, date_str, ts_label):
    creds = Credentials.from_service_account_file(SA_FILE, scopes=SCOPES)
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(SHEET_ID)

    tab_name = f"🔴 Today's Live {date_label(date_str)}"
    existing = [w.title for w in sh.worksheets()]
    if tab_name in existing:
        sh.del_worksheet(sh.worksheet(tab_name))
    ws = sh.add_worksheet(title=tab_name, rows=max(500, len(rows) + 50), cols=8)
    sh.reorder_worksheets([ws] + [w for w in sh.worksheets() if w.title != tab_name])

    # Pad every row to exactly 6 cols (sheet has 8 — 2 spare)
    padded = [r + [''] * max(0, 6 - len(r)) for r in rows]
    ws.update(range_name='A1', values=padded, value_input_option='USER_ENTERED')

    # Format section headers + column headers
    sheet_id = ws.id
    def rgb(r, g, b):
        return {'red': r/255, 'green': g/255, 'blue': b/255}

    fmt_reqs = []
    for i, row in enumerate(padded, 1):
        if not row or not row[0]:
            continue
        v = str(row[0])
        if v.startswith(('📊', '🎯', '🎨', '📐')):
            fmt_reqs.append({'repeatCell': {
                'range': {'sheetId': sheet_id, 'startRowIndex': i-1, 'endRowIndex': i,
                          'startColumnIndex': 0, 'endColumnIndex': 6},
                'cell': {'userEnteredFormat': {
                    'backgroundColor': rgb(30, 60, 120),
                    'textFormat': {'bold': True, 'fontSize': 11, 'foregroundColor': rgb(255, 255, 255)},
                }},
                'fields': 'userEnteredFormat(backgroundColor,textFormat)',
            }})
        elif v.startswith('━━'):
            fmt_reqs.append({'repeatCell': {
                'range': {'sheetId': sheet_id, 'startRowIndex': i-1, 'endRowIndex': i,
                          'startColumnIndex': 0, 'endColumnIndex': 6},
                'cell': {'userEnteredFormat': {
                    'backgroundColor': rgb(52, 73, 94),
                    'textFormat': {'bold': True, 'foregroundColor': rgb(255, 255, 255)},
                }},
                'fields': 'userEnteredFormat(backgroundColor,textFormat)',
            }})
        elif v in ('Audience', 'Bucket', 'Rank', 'Creative Type'):
            fmt_reqs.append({'repeatCell': {
                'range': {'sheetId': sheet_id, 'startRowIndex': i-1, 'endRowIndex': i,
                          'startColumnIndex': 0, 'endColumnIndex': 6},
                'cell': {'userEnteredFormat': {
                    'backgroundColor': rgb(70, 70, 70),
                    'textFormat': {'bold': True, 'foregroundColor': rgb(255, 255, 255)},
                }},
                'fields': 'userEnteredFormat(backgroundColor,textFormat)',
            }})

    # Column widths
    fmt_reqs.append({'updateDimensionProperties': {
        'range': {'sheetId': sheet_id, 'dimension': 'COLUMNS', 'startIndex': 0, 'endIndex': 1},
        'properties': {'pixelSize': 380}, 'fields': 'pixelSize',
    }})

    if fmt_reqs:
        for chunk in range(0, len(fmt_reqs), 200):
            sh.batch_update({'requests': fmt_reqs[chunk:chunk+200]})

    print(f"  ✅ Wrote {len(padded)} rows to '{tab_name}'")


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--date', default=datetime.now(IST).strftime('%Y-%m-%d'),
                        help="YYYY-MM-DD (default: today IST)")
    args = parser.parse_args()

    if not TOKEN:
        print("META_ACCESS_TOKEN not set — aborting"); sys.exit(2)

    now      = datetime.now(IST)
    ts_label = now.strftime('%d %b %Y, %H:%M IST')

    print(f"\n🔴 Today's Live Report — {args.date}")
    print(f"   Run timestamp: {ts_label}\n")

    print("→ Fetching campaign-level today data…")
    camps = fetch_today_campaigns(args.date)
    print(f"   {len(camps)} SM camps with spend today")

    print("→ Fetching ad-level today data…")
    ads = fetch_today_ads(args.date)
    print(f"   {len(ads)} SM ads with spend today")

    rows = []
    rows += section_audience_breakdown(camps, ts_label)
    rows += section_success_rate(camps, ts_label)
    rows += section_best_creatives(ads, ts_label)

    write(rows, args.date, ts_label)


if __name__ == '__main__':
    main()
