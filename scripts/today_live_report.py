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

Cadence: every hour during IST business hours 09:30–19:30 IST (managed by today-live.yml).
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
    """Ad-level today insights for the creative section. Includes ONLY ads
    that had spend > 0 on date_str (today). Use fetch_active_ads() for the
    universe of currently-active ads regardless of spend."""
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


def fetch_active_ads():
    """Currently-active ads in the SM portal (effective_status=ACTIVE — matches
    what the user sees in Meta Ads Manager). Used for accurate # Active Ads
    counts in the Creative Type Mix section.

    Note: an ad that was paused mid-day after spending will appear in
    fetch_today_ads() (it had spend) but NOT in fetch_active_ads(). That's
    why the two counts can diverge in either direction."""
    out = []
    for acct, acct_name in ACCOUNTS:
        if not acct:
            continue
        rows = paginate(f"{GRAPH}/{acct}/ads", {
            'fields': 'id,name,effective_status,status',
            'effective_status': '["ACTIVE"]',
            'limit': 500,
        })
        for r in rows:
            name = r.get('name', '')
            out.append({
                'aid':           r.get('id'),
                'account':       acct_name,
                'name':          name,
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


def section_best_creatives(ads, ts_label, top_n=10, min_spend=500, active_ads=None):
    """`ads` = ads with spend>0 today. `active_ads` = currently-active ads
    (effective_status=ACTIVE, regardless of spend)."""
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

    # Creative type mix — Active count (Meta UI) vs Had-Spend count (today insights)
    rows.append([f"📐  CREATIVE TYPE MIX  |  Active = currently active in Meta · Had Spend = spent > 0 today",
                 '', '', '', '', ''])
    rows.append(['Creative Type', '# Active', '# Had Spend', 'Spend (₹)', '% of Spend', 'Avg ROAS'])

    spent_agg = defaultdict(lambda: {'spent_count': 0, 'spend': 0.0, 'rev': 0.0})
    for a in ads:
        t = spent_agg[a['creative_type']]
        t['spent_count'] += 1
        t['spend'] += a['spend']
        t['rev']   += a['spend'] * a['roas']
    active_agg = defaultdict(int)
    if active_ads is not None:
        for a in active_ads:
            active_agg[a['creative_type']] += 1

    total = sum(t['spend'] for t in spent_agg.values())
    order = ['Paras', 'Partnership', 'Motion', 'Static', 'Catalogue', 'Testing', 'Others']
    for ct in order:
        if ct not in spent_agg and ct not in active_agg:
            continue
        s = spent_agg.get(ct, {'spent_count': 0, 'spend': 0.0, 'rev': 0.0})
        active_count = active_agg.get(ct, 0)
        roas  = (s['rev'] / s['spend']) if s['spend'] else 0.0
        share = (s['spend'] / total * 100) if total else 0.0
        rows.append([
            ct,
            active_count,
            s['spent_count'],
            int(s['spend']),
            fmt_pct(share),
            fmt_roas(roas),
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


# ── HTML dashboard render ─────────────────────────────────────────────────────
def _roas_class(r):
    if r >= 2.0: return 'rg'
    if r >= 1.5: return 'ro'
    return 'rr'


def compute_today_aggregates(camps, ads, active_ads=None):
    """Pure data: turn raw camps + ads into a structured dict ready to render.
    Reused by render_html() (standalone HTML) and by auto_rebuild_dashboard.py
    (which embeds today's sections inside the NTN dashboard)."""
    total_spend  = sum(c['spend']  for c in camps)
    total_budget = sum(c['budget'] for c in camps)
    total_camps  = len(camps)
    total_rev    = sum(c['spend'] * c['roas'] for c in camps)
    avg_roas     = (total_rev / total_spend) if total_spend else 0.0

    spend_below_1   = sum(c['spend'] for c in camps if c['roas'] < 1.0)
    spend_above_1_5 = sum(c['spend'] for c in camps if c['roas'] >= 1.5)
    pct_below_1     = (spend_below_1   / total_spend * 100) if total_spend else 0
    pct_above_1_5   = (spend_above_1_5 / total_spend * 100) if total_spend else 0

    bucket_data = [
        ('Below 1.0x', sum(c['spend'] for c in camps if c['roas'] < 1.0),  '#dc3545'),
        ('≥ 1.0x',     sum(c['spend'] for c in camps if c['roas'] >= 1.0), '#f5c518'),
        ('≥ 1.5x',     sum(c['spend'] for c in camps if c['roas'] >= 1.5), '#90c695'),
        ('≥ 1.75x',    sum(c['spend'] for c in camps if c['roas'] >= 1.75), '#5fa572'),
        ('≥ 2.0x',     sum(c['spend'] for c in camps if c['roas'] >= 2.0), '#00a878'),
        ('≥ 3.0x',     sum(c['spend'] for c in camps if c['roas'] >= 3.0), '#9b59b6'),
    ]

    def aggregate_audiences(type_key):
        agg = defaultdict(lambda: {'count': 0, 'spend': 0.0, 'budget': 0.0, 'rev': 0.0})
        for c in camps:
            if c['type'] != type_key:
                continue
            seg = c['segment'] or 'Unmapped'
            a = agg[seg]
            a['count']  += 1
            a['spend']  += c['spend']
            a['budget'] += c['budget']
            a['rev']    += c['spend'] * c['roas']
        out = []
        for seg, a in sorted(agg.items(), key=lambda kv: -kv[1]['spend']):
            roas = (a['rev'] / a['spend']) if a['spend'] else 0.0
            out.append({'seg': seg, 'count': a['count'], 'spend': int(a['spend']),
                        'budget': int(a['budget']), 'roas': roas})
        return out

    prospecting = aggregate_audiences('Sales')
    retarget    = aggregate_audiences('Retarget')

    triggered = []
    for c in camps:
        if c['budget'] <= 0:
            continue
        pct_spent = c['spend'] / c['budget'] * 100
        if pct_spent >= 30 and c['roas'] <= 1.0:
            triggered.append({**c, 'pct_spent': pct_spent})
    triggered.sort(key=lambda x: -x['pct_spent'])

    eligible_ads = [a for a in ads if a['spend'] >= 500]
    eligible_ads.sort(key=lambda x: -x['roas'])
    top_creatives = eligible_ads[:10]

    # Spend / ROAS / spent-today count comes from `ads` (had spend > 0 today).
    # Active count comes from `active_ads` (effective_status=ACTIVE in Meta UI
    # right now). The two universes overlap but aren't identical: an ad paused
    # mid-day after spending is in `ads` but not `active_ads`; a brand-new
    # active ad with no spend yet is the reverse.
    spent_agg = defaultdict(lambda: {'spent_count': 0, 'spend': 0.0, 'rev': 0.0})
    for a in ads:
        t = spent_agg[a['creative_type']]
        t['spent_count'] += 1
        t['spend'] += a['spend']
        t['rev']   += a['spend'] * a['roas']

    active_agg = defaultdict(lambda: {'active_count': 0})
    if active_ads is not None:
        for a in active_ads:
            active_agg[a['creative_type']]['active_count'] += 1

    total_ad_spend = sum(t['spend'] for t in spent_agg.values())
    creative_mix = []
    for ct in ['Paras', 'Partnership', 'Motion', 'Static', 'Catalogue', 'Testing', 'Others']:
        if ct not in spent_agg and ct not in active_agg:
            continue
        s = spent_agg.get(ct, {'spent_count': 0, 'spend': 0.0, 'rev': 0.0})
        a = active_agg.get(ct, {'active_count': 0})
        roas  = (s['rev'] / s['spend']) if s['spend'] else 0.0
        share = (s['spend'] / total_ad_spend * 100) if total_ad_spend else 0.0
        creative_mix.append({
            'type':         ct,
            'active_count': a['active_count'],
            'spent_count':  s['spent_count'],
            'spend':        int(s['spend']),
            'share':        share,
            'roas':         roas,
        })

    return {
        'total_spend': total_spend, 'total_budget': total_budget, 'total_camps': total_camps,
        'avg_roas': avg_roas,
        'spend_below_1': spend_below_1, 'spend_above_1_5': spend_above_1_5,
        'pct_below_1': pct_below_1, 'pct_above_1_5': pct_above_1_5,
        'bucket_data': bucket_data,
        'prospecting': prospecting, 'retarget': retarget,
        'triggered': triggered,
        'top_creatives': top_creatives, 'creative_mix': creative_mix,
    }


def render_today_styles():
    """The CSS rules used by render_today_inner(). Standalone — auto_rebuild_dashboard
    appends this to its own stylesheet so the embedded today-live sections render right."""
    return """
.tl-kpi-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-bottom:18px}
.tl-kpi{background:#fff;border-radius:12px;padding:18px;border:1px solid #dde3f0;box-shadow:0 2px 10px rgba(0,0,0,.04);position:relative;overflow:hidden;transition:transform .15s}
.tl-kpi:hover{transform:translateY(-2px)}
.tl-kpi::before{content:'';position:absolute;top:0;left:0;width:4px;height:100%}
.tl-kpi.spend::before{background:#f5c518}
.tl-kpi.camps::before{background:#1a3d7c}
.tl-kpi.good::before{background:#00a878}
.tl-kpi.bad::before{background:#dc3545}
.tl-kpi-icon{font-size:22px;margin-bottom:6px}
.tl-kpi-lbl{font-size:10px;color:#6b7280;text-transform:uppercase;letter-spacing:.8px;font-weight:700}
.tl-kpi-val{font-size:24px;font-weight:900;color:#0d2145;margin:5px 0 3px}
.tl-kpi-sub{font-size:11px;color:#6b7280}
.tl-card{background:#fff;border-radius:12px;padding:18px;border:1px solid #dde3f0;box-shadow:0 2px 10px rgba(0,0,0,.04);margin-bottom:18px}
.tl-sec-ttl{font-size:13px;font-weight:700;color:#0d2145;margin-bottom:14px;padding-bottom:8px;border-bottom:2px solid #eef2ff}
.tl-sec-ttl .meta{color:#6b7280;font-weight:400;font-size:11px;margin-left:8px}
.tl-tables-row{display:grid;grid-template-columns:1fr 1fr;gap:18px;margin-bottom:18px}
@media(max-width:900px){.tl-kpi-grid,.tl-tables-row{grid-template-columns:1fr}}
.tl-bucket-row{display:grid;grid-template-columns:90px 1fr 200px;gap:12px;align-items:center;padding:6px 0}
.tl-bucket-lbl{font-size:12px;font-weight:700;color:#0d2145}
.tl-bucket-bar-wrap{height:22px;background:#f4f6fa;border-radius:6px;overflow:hidden}
.tl-bucket-bar{height:100%;border-radius:6px;transition:width .4s}
.tl-bucket-val{font-size:11px;color:#6b7280;text-align:right;font-variant-numeric:tabular-nums}
.tl-alert{background:#fff5f5;border:1px solid #fed7d7;border-radius:12px;padding:18px;margin-bottom:18px}
.tl-alert-ttl{color:#a3260a;font-weight:800;margin-bottom:10px;font-size:13px;padding-bottom:8px;border-bottom:1px solid #fed7d7}
.tl-section table{width:100%;border-collapse:collapse;font-size:12px}
.tl-section th{background:#0d2145;color:#fff;padding:8px 10px;text-align:left;font-size:10px;letter-spacing:.3px;text-transform:uppercase}
.tl-section th:not(:first-child){text-align:center}
.tl-section td{padding:7px 10px;border-bottom:1px solid #eef2ff}
.tl-section td:not(:first-child){text-align:center;font-weight:600}
.tl-section .rg{color:#0d6e3a;font-weight:700}
.tl-section .ro{color:#a35a00;font-weight:700}
.tl-section .rr{color:#a3260a;font-weight:700}
"""


def render_today_inner(agg, date_str, ts_label, sheet_url, heading=None):
    """Render the today-live body sections only (no <html>/<head>/<body> chrome).
    `heading`: optional label text for an in-line section header. Pass None to
    omit (used when the host dashboard already provides its own page title)."""
    # Audience rows
    def audience_rows(items, total):
        out = []
        for it in items:
            share = (it['spend'] / total * 100) if total else 0
            cls = _roas_class(it['roas'])
            out.append(
                f"<tr><td>{it['seg']}</td><td>{it['count']}</td>"
                f"<td>₹{it['budget']:,}</td><td>₹{it['spend']:,}</td>"
                f"<td>{share:.1f}%</td><td class='{cls}'>{it['roas']:.2f}x</td></tr>"
            )
        return ''.join(out)

    prospecting_rows = audience_rows(agg['prospecting'], sum(it['spend'] for it in agg['prospecting']))
    retarget_rows    = audience_rows(agg['retarget'],    sum(it['spend'] for it in agg['retarget']))

    cr_rows = ''
    for i, a in enumerate(agg['top_creatives'], 1):
        cls = _roas_class(a['roas'])
        cr_rows += (f"<tr><td><b>{i}</b></td><td style='font-size:11px'>{a['name'][:80]}</td>"
                    f"<td>{a['creative_type']}</td><td style='font-size:11px'>{a['account']}</td>"
                    f"<td>₹{int(a['spend']):,}</td><td class='{cls}'>{a['roas']:.2f}x</td></tr>")

    cm_rows = ''
    for it in agg['creative_mix']:
        cls = _roas_class(it['roas'])
        cm_rows += (
            f"<tr><td>{it['type']}</td>"
            f"<td>{it['active_count']}</td>"
            f"<td>{it['spent_count']}</td>"
            f"<td>₹{it['spend']:,}</td>"
            f"<td>{it['share']:.1f}%</td>"
            f"<td class='{cls}'>{it['roas']:.2f}x</td></tr>"
        )

    tr_rows = ''
    if agg['triggered']:
        for c in agg['triggered'][:50]:
            zero_flag = '💀 ZERO' if c['roas'] == 0 else f"{c['roas']:.2f}x"
            tr_rows += (f"<tr><td style='font-size:11px'>{c['name'][:90]}</td>"
                        f"<td>{c['account']}</td><td>{c['type']}</td>"
                        f"<td>₹{int(c['budget']):,}</td><td>₹{int(c['spend']):,}</td>"
                        f"<td><b>{c['pct_spent']:.0f}%</b></td>"
                        f"<td style='color:#dc3545;font-weight:700'>{zero_flag}</td></tr>")
    else:
        tr_rows = "<tr><td colspan='7' style='text-align:center;color:#0d6e3a;padding:20px'>✅ No camps currently triggering the close decision rule.</td></tr>"

    max_b = max(b[1] for b in agg['bucket_data']) or 1
    bucket_html = ''
    for label, val, color in agg['bucket_data']:
        share = (val / agg['total_spend'] * 100) if agg['total_spend'] else 0
        bar_w = (val / max_b) * 100
        bucket_html += (
            f"<div class='tl-bucket-row'>"
            f"<div class='tl-bucket-lbl'>{label}</div>"
            f"<div class='tl-bucket-bar-wrap'>"
            f"<div class='tl-bucket-bar' style='width:{bar_w:.1f}%;background:{color}'></div>"
            f"</div>"
            f"<div class='tl-bucket-val'>₹{int(val):,} ({share:.1f}%)</div>"
            f"</div>"
        )

    heading_html = ''
    if heading:
        heading_html = f'<h2 style="font-size:18px;color:#0d2145;margin-bottom:14px">{heading}</h2>'

    triggered_count = len(agg['triggered'])
    triggered_label = 'camp' if triggered_count == 1 else 'camps'

    return f"""
<div class="tl-section">
  {heading_html}

  <div class="tl-kpi-grid">
    <div class="tl-kpi spend">
      <div class="tl-kpi-icon">💰</div><div class="tl-kpi-lbl">Total Spend Today</div>
      <div class="tl-kpi-val">₹{int(agg['total_spend']):,}</div>
      <div class="tl-kpi-sub">of ₹{int(agg['total_budget']):,} budgeted</div>
    </div>
    <div class="tl-kpi camps">
      <div class="tl-kpi-icon">📦</div><div class="tl-kpi-lbl">Active Camps</div>
      <div class="tl-kpi-val">{agg['total_camps']}</div>
      <div class="tl-kpi-sub">with spend &gt; 0</div>
    </div>
    <div class="tl-kpi good">
      <div class="tl-kpi-icon">🟢</div><div class="tl-kpi-lbl">Spend ≥ 1.5x ROAS</div>
      <div class="tl-kpi-val">{agg['pct_above_1_5']:.1f}%</div>
      <div class="tl-kpi-sub">₹{int(agg['spend_above_1_5']):,} / day</div>
    </div>
    <div class="tl-kpi bad">
      <div class="tl-kpi-icon">🔴</div><div class="tl-kpi-lbl">Spend &lt; 1.0x ROAS</div>
      <div class="tl-kpi-val">{agg['pct_below_1']:.1f}%</div>
      <div class="tl-kpi-sub">₹{int(agg['spend_below_1']):,} on close watch</div>
    </div>
  </div>

  <div class="tl-card">
    <div class="tl-sec-ttl">🎯 Daily Success Rate <span class="meta">— spend distribution by ROAS bucket (avg ROAS today: <b>{agg['avg_roas']:.2f}x</b>)</span></div>
    {bucket_html}
  </div>

  <div class="tl-alert">
    <div class="tl-alert-ttl">💀 DECISION TRIGGERED — ≥30% budget spent + ROAS ≤ 1x ({triggered_count} {triggered_label})</div>
    <table>
      <thead><tr>
        <th>Campaign</th><th>Account</th><th>Type</th>
        <th>Budget</th><th>Spent</th><th>% Spent</th><th>ROAS</th>
      </tr></thead>
      <tbody>{tr_rows}</tbody>
    </table>
  </div>

  <div class="tl-tables-row">
    <div class="tl-card">
      <div class="tl-sec-ttl">🎯 Prospecting <span class="meta">— audiences by spend</span></div>
      <table>
        <thead><tr><th>Audience</th><th># Camps</th><th>Budget</th><th>Spent</th><th>% Block</th><th>ROAS</th></tr></thead>
        <tbody>{prospecting_rows or '<tr><td colspan="6" style="text-align:center;color:#888;padding:20px">No prospecting camps active</td></tr>'}</tbody>
      </table>
    </div>
    <div class="tl-card">
      <div class="tl-sec-ttl">🔁 Retarget <span class="meta">— audiences by spend</span></div>
      <table>
        <thead><tr><th>Audience</th><th># Camps</th><th>Budget</th><th>Spent</th><th>% Block</th><th>ROAS</th></tr></thead>
        <tbody>{retarget_rows or '<tr><td colspan="6" style="text-align:center;color:#888;padding:20px">No retarget camps active</td></tr>'}</tbody>
      </table>
    </div>
  </div>

  <div class="tl-tables-row">
    <div class="tl-card">
      <div class="tl-sec-ttl">🎨 Best Creatives Today <span class="meta">— top 10 ads by ROAS (min ₹500 spend)</span></div>
      <table>
        <thead><tr><th>#</th><th>Ad Name</th><th>Type</th><th>Account</th><th>Spent</th><th>ROAS</th></tr></thead>
        <tbody>{cr_rows or '<tr><td colspan="6" style="text-align:center;color:#888;padding:20px">No qualifying ads yet</td></tr>'}</tbody>
      </table>
    </div>
    <div class="tl-card">
      <div class="tl-sec-ttl">📐 Creative Type Mix <span class="meta">— Active = currently active in Meta · Had-spend = spent &gt; 0 today (any status)</span></div>
      <table>
        <thead><tr><th>Type</th><th># Active</th><th># Had Spend</th><th>Spent (₹)</th><th>% of Spend</th><th>Avg ROAS</th></tr></thead>
        <tbody>{cm_rows or '<tr><td colspan="6" style="text-align:center;color:#888;padding:20px">No data</td></tr>'}</tbody>
      </table>
    </div>
  </div>
</div>
"""


def render_html(camps, ads, date_str, ts_label, active_ads=None):
    """Build a single-page HTML dashboard mirroring the NTN dashboard's style.
    Output goes to out/today_live.html (always-current) AND out/today_live_<date>.html.
    `active_ads` is optional; when passed, the Creative Type Mix section shows
    real active counts alongside spent-today counts.
    """
    agg = compute_today_aggregates(camps, ads, active_ads=active_ads)
    sheet_url = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}"
    inner = render_today_inner(agg, date_str, ts_label, sheet_url)
    styles = render_today_styles()

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Today's Live — Meta Ads</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:'Segoe UI',system-ui,sans-serif;background:#f0f4fb;color:#1a1a2e}}
.header{{background:linear-gradient(135deg,#0d2145,#1a3d7c);padding:20px 28px;display:flex;align-items:center;justify-content:space-between}}
.header h1{{color:#fff;font-size:20px;font-weight:700}}
.header p{{color:rgba(255,255,255,.6);font-size:11px;margin-top:3px}}
.last-upd{{color:rgba(255,255,255,.6);font-size:11px;text-align:right}}
.last-upd a{{color:rgba(255,255,255,.85);text-decoration:none;border-bottom:1px dashed rgba(255,255,255,.4)}}
.main{{max-width:1400px;margin:0 auto;padding:24px 28px}}
footer{{text-align:center;color:#6b7280;font-size:11px;padding:20px}}
footer a{{color:#1a3d7c;text-decoration:none;border-bottom:1px dashed #1a3d7c}}
{styles}
</style>
</head>
<body>

<div class="header">
  <div>
    <h1>🔴 Today's Live — Meta Ads</h1>
    <p>{datetime.strptime(date_str,'%Y-%m-%d').strftime('%A, %d %B %Y')} · SM portal · Live data from Meta Marketing API</p>
  </div>
  <div style="display:flex;flex-direction:column;align-items:flex-end;gap:6px">
    <nav style="font-size:12px">
      <a href="/" style="color:rgba(255,255,255,.85);text-decoration:none;border-bottom:1px dashed rgba(255,255,255,.5);margin-right:14px">🏠 NTN Dashboard</a>
      <a href="#" style="color:#fff;font-weight:700;border-bottom:2px solid #fff;text-decoration:none;margin-right:14px">🔴 Today Live</a>
      <a href="/categories" style="color:rgba(255,255,255,.85);text-decoration:none;border-bottom:1px dashed rgba(255,255,255,.5)">📂 Category Heads</a>
    </nav>
    <div class="last-upd">
      Last updated: {ts_label} · <a href="{sheet_url}" target="_blank">Open in Sheets ↗</a>
    </div>
  </div>
</div>

<div class="main">{inner}</div>

<footer>
  🤖 Auto-rebuilds hourly during IST business hours (09:30–19:30) · <a href="{sheet_url}" target="_blank">Sheet source</a> · {ts_label}
</footer>

</body>
</html>
"""



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

    print("→ Fetching ad-level today data (had spend > 0)…")
    ads = fetch_today_ads(args.date)
    print(f"   {len(ads)} SM ads with spend today")

    print("→ Fetching currently-active SM ads (matches Meta UI count)…")
    active_ads = fetch_active_ads()
    print(f"   {len(active_ads)} SM ads currently active")

    rows = []
    rows += section_audience_breakdown(camps, ts_label)
    rows += section_success_rate(camps, ts_label)
    rows += section_best_creatives(ads, ts_label, active_ads=active_ads)

    write(rows, args.date, ts_label)

    # Also produce an HTML dashboard alongside the sheet update.
    print("→ Rendering HTML dashboard…")
    html = render_html(camps, ads, args.date, ts_label, active_ads=active_ads)
    out_now    = OUT_DIR / 'today_live.html'                     # always-current (overwritten)
    out_dated  = OUT_DIR / f'today_live_{args.date}.html'        # date-stamped history
    out_now.write_text(html, encoding='utf-8')
    out_dated.write_text(html, encoding='utf-8')
    print(f"   ✅ {out_now}")
    print(f"   ✅ {out_dated}")


if __name__ == '__main__':
    main()
