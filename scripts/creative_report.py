"""
Creative Performance Report — Ad Level
Creates tab: 📊 Creative Report [DATE]

Sections:
  1. Main Creative Report  — Spend / % / Count / 1D ROAS / 7D ROAS by creative type
  2. Testing Report        — Same + Day-wise age breakdown (Day 0 / 1-3 / 4-7 / 7+)

Creative Types:
  Paras       → ad name contains 'paras'
  Partnership → ad name contains 'partnership'
  Motion      → ad name contains 'reel', 'clp', 'motion', 'video', 'inde'
  Static      → ad name contains 'static'
  Catalogue   → ad name contains 'catalog' or 'catalogue'
  Testing     → ad name starts with 'testing_' or 'test_' (goes to Testing section)
  Others      → everything else

Usage:
  python3 creative_report.py --date 2026-04-19
  python3 creative_report.py --date 2026-04-19 --accounts SM_FRAGRANCE_01,SML_SKIN
"""

import os, sys, json, argparse, requests
from pathlib import Path
import gspread
from datetime import datetime, timedelta
from collections import defaultdict
from google.oauth2.service_account import Credentials
from dotenv import load_dotenv

# ── Path setup (Phase 2: GitHub-Actions-friendly paths) ──────────────────────
_REPO_ROOT = Path(__file__).resolve().parent.parent
STATE_DIR  = Path(os.environ.get('META_REPORTS_STATE_DIR') or (_REPO_ROOT / 'state'))
OUT_DIR    = Path(os.environ.get('META_REPORTS_OUT_DIR')   or (_REPO_ROOT / 'out'))
STATE_DIR.mkdir(parents=True, exist_ok=True)
OUT_DIR.mkdir(parents=True, exist_ok=True)
load_dotenv(_REPO_ROOT / '.env')

TOKEN    = os.getenv('META_ACCESS_TOKEN')
SA_FILE  = os.environ.get('GOOGLE_SERVICE_ACCOUNT_FILE') or str(_REPO_ROOT / 'google-service-account.json')
SHEET_ID = '11IAPsJlil75aehYf5IzpSaTCLcAgPk9-57p6ZuPNNQM'
GRAPH    = 'https://graph.facebook.com/v19.0'

SCOPES = ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive']

ACCOUNT_MAP = {
    'SM_FRAGRANCE_01':  (os.getenv('SM_FRAGRANCE_01'), 'SM'),
    'SM_SKIN':          (os.getenv('SM_SKIN'),         'SM'),
    'SM_HAIR':          (os.getenv('SM_HAIR'),         'SM'),
    'SM_CRYSTALS':      (os.getenv('SM_CRYSTALS'),     'SM'),
    'SM_PERFUME':       (os.getenv('SM_PERFUME'),      'SM'),
    'SM_CL_05':         (os.getenv('SM_CREDIT_LINE_05'), 'SM'),
    'SM_CL_06':         (os.getenv('SM_CREDIT_LINE_06'), 'SM'),
    'SML_SKIN':         (os.getenv('SML_SKIN'),        'SML'),
    'SML_HAIR':         (os.getenv('SML_HAIR'),        'SML'),
    'SML_CRYSTALS':     (os.getenv('SML_CRYSTALS'),    'SML'),
    'SML_CL_06':        (os.getenv('SML_CL_06'),       'SML'),
    'SML_CL_07':        (os.getenv('SML_CL_07'),       'SML'),
    'NBP_SKIN':         (os.getenv('NBP_SKIN'),        'NBP'),
    'NBP_HAIR_PERFUME': (os.getenv('NBP_HAIR_PERFUME'),'NBP'),
    'NBP_CRYSTALS':     (os.getenv('NBP_CRYSTALS'),    'NBP'),
}

CREATIVE_TYPES = ['Paras', 'Partnership', 'Motion', 'Static', 'Catalogue', 'Others']
DAY_BUCKETS    = ['Day 0', 'Day 1–3', 'Day 4–7', 'Day 7+']

# ── Colors ────────────────────────────────────────────────────────────────────
def rgb(r, g, b): return {'red': r/255, 'green': g/255, 'blue': b/255}

SECTION_BG   = rgb(30,  30,  30)
COL_HDR_BG   = rgb(52,  73,  94)
WHITE        = rgb(255, 255, 255)
PURPLE_BG    = rgb(142,  68, 173)
PURPLE_LIGHT = rgb(243, 232, 255)
ZEBRA_ODD    = rgb(245, 247, 250)
ZEBRA_EVEN   = rgb(255, 255, 255)
TOTAL_BG     = rgb(236, 240, 241)
SUBTOTAL_BG  = rgb(248, 249, 250)
ORANGE_LIGHT = rgb(253, 245, 230)

TYPE_COLORS = {
    'Paras':       rgb(155,  89, 182),
    'Partnership': rgb( 41, 128, 185),
    'Motion':      rgb( 39, 174,  96),
    'Static':      rgb(243, 156,  18),
    'Catalogue':   rgb(231,  76,  60),
    'Others':      rgb(127, 140, 141),
    'Testing':     rgb( 22, 160, 133),
}

# ── Helpers ───────────────────────────────────────────────────────────────────
def paginate(endpoint, params):
    params['access_token'] = TOKEN
    results = []
    r = requests.get(f"{GRAPH}/{endpoint}", params=params, timeout=30)
    data = r.json()
    if 'error' in data:
        print(f"  ⚠️  API error: {data['error'].get('message','')}")
        return []
    results.extend(data.get('data', []))
    while 'paging' in data and 'next' in data.get('paging', {}):
        data = requests.get(data['paging']['next'], timeout=30).json()
        results.extend(data.get('data', []))
    return results

def classify_creative(ad_name):
    n = ad_name.lower()
    if n.startswith('testing_') or n.startswith('test_'):
        return 'Testing'
    if 'paras' in n:
        return 'Paras'
    if 'partnership' in n:
        return 'Partnership'
    if any(k in n for k in ['catalog', 'catalogue']):
        return 'Catalogue'
    if 'static' in n:
        return 'Static'
    if any(k in n for k in ['reel', 'clp', 'motion', 'video', 'inde']):
        return 'Motion'
    return 'Others'

def day_bucket(ad_created_time, report_date):
    """Days since ad was created → bucket."""
    try:
        created = datetime.strptime(ad_created_time[:10], '%Y-%m-%d')
        days = (report_date - created).days
        if days == 0:   return 'Day 0'
        if days <= 3:   return 'Day 1–3'
        if days <= 7:   return 'Day 4–7'
        return 'Day 7+'
    except:
        return 'Day 7+'

def get_roas(row, window):
    for entry in row.get('purchase_roas', []):
        if entry.get('action_type') == 'omni_purchase':
            v = entry.get(window, '')
            if v:
                return round(float(v), 2)
    return None

WHITE_C = rgb(255, 255, 255)

def fmt(v):
    if v is None or v == 0 or v == '': return ''
    return f"{float(v):,.0f}"

def fmt_delta_creative(curr, prev, is_roas=False):
    try:
        c = float(curr) if curr not in (None,'') else None
        p = float(prev) if prev not in (None,'') else None
        if c is None or p is None: return ''
        d = c - p
        if d == 0: return '—'
        arrow = '▲' if d > 0 else '▼'
        return f"{arrow} {abs(d):.2f}x" if is_roas else f"{arrow} {abs(d):,.0f}"
    except: return ''

def delta_color_c(curr, prev, higher_is_better=True):
    try:
        c, p = float(curr), float(prev)
        if c > p: return rgb(39,174,96) if higher_is_better else rgb(231,76,60)
        if c < p: return rgb(231,76,60) if higher_is_better else rgb(39,174,96)
        return rgb(200,200,200)
    except: return rgb(200,200,200)

def fmt_pct(v, total):
    if not total or not v: return ''
    return f"{v/total*100:.1f}%"

def fmt_roas(v):
    if v is None or v == '': return ''
    return f"{float(v):.2f}x"

def w_roas(rows, window):
    """Weighted ROAS = sum(roas*spend) / sum(spend) for rows with valid roas."""
    total_rev = total_sp = 0
    for r in rows:
        sp = float(r.get('spend', 0) or 0)
        v  = r.get(f'roas_{window}')
        if v and sp:
            total_rev += v * sp
            total_sp  += sp
    return round(total_rev / total_sp, 2) if total_sp else None

# ── Fetch ad data ─────────────────────────────────────────────────────────────
def fetch_ads(account_id, date_str):
    date_obj = datetime.strptime(date_str, '%Y-%m-%d')

    # Ad insights for the date (1d_click + 7d_click)
    print(f"  Fetching ad insights for {account_id}...")
    insight_rows = paginate(f"{account_id}/insights", {
        'level': 'ad',
        'fields': 'ad_id,ad_name,adset_name,campaign_name,spend,purchase_roas',
        'action_attribution_windows': json.dumps(['1d_click', '7d_click']),
        'time_range': json.dumps({'since': date_str, 'until': date_str}),
        'limit': 500,
        'sort': 'spend_descending',
    })
    print(f"  Got {len(insight_rows)} ads with spend")
    if not insight_rows:
        return []

    # Get ad creation dates (needed for day bucket on testing ads)
    ad_ids = [r['ad_id'] for r in insight_rows]
    print(f"  Fetching ad creation dates...")
    creation_map = {}
    # Batch in groups of 50
    for i in range(0, len(ad_ids), 50):
        batch = ad_ids[i:i+50]
        for aid in batch:
            r = requests.get(f"{GRAPH}/{aid}", params={
                'access_token': TOKEN,
                'fields': 'id,created_time',
            }, timeout=15)
            d = r.json()
            if 'created_time' in d:
                creation_map[aid] = d['created_time'][:10]

    # Build enriched rows
    result = []
    for row in insight_rows:
        spend = float(row.get('spend', 0) or 0)
        if spend == 0:
            continue
        ad_id   = row['ad_id']
        ad_name = row.get('ad_name', '')
        created = creation_map.get(ad_id, date_str)
        result.append({
            'ad_id':        ad_id,
            'ad_name':      ad_name,
            'adset_name':   row.get('adset_name', ''),
            'campaign_name':row.get('campaign_name', ''),
            'spend':        spend,
            'creative_type': classify_creative(ad_name),
            'day_bucket':   day_bucket(created, date_obj),
            'created':      created,
            'roas_1d':      get_roas(row, '1d_click'),
            'roas_7d':      get_roas(row, '7d_click'),
        })
    return result

# ── Build sheet ───────────────────────────────────────────────────────────────
def load_prev_creative(sh, date_str):
    """Load previous day's creative report data from the sheet tab."""
    try:
        prev_date = (datetime.strptime(date_str, '%Y-%m-%d') - timedelta(days=1))
        d = prev_date
        prev_tab = f"📊 Creative Report {d.day} {d.strftime('%b').upper()} {d.strftime('%y')}"
        ws = sh.worksheet(prev_tab)
        data = ws.get_all_values()
        # Parse main creative section rows (after header, before testing section)
        # Format: Type | Spend | % | Count | 1D ROAS | 7D ROAS | Avg/Ad
        prev = {}
        for row in data:
            if not row or not row[0]: continue
            ctype = row[0].strip()
            if ctype in CREATIVE_TYPES + ['Testing']:
                try:
                    prev[ctype] = {
                        'spend': float(str(row[1]).replace(',','')) if row[1] else 0,
                        'count': int(row[3]) if row[3] else 0,
                        'roas_1d': float(row[4].replace('x','')) if row[4] and 'x' in row[4] else None,
                        'roas_7d': float(row[5].replace('x','')) if row[5] and 'x' in row[5] else None,
                    }
                except:
                    pass
        print(f"  📦 Prev creative data: {list(prev.keys())}")
        return prev
    except Exception as e:
        print(f"  ⚠️  No prev creative tab: {e}")
        return {}


def build_report(all_ads, date_str, prev_creative=None):
    """Returns (sheet_data, fmt_reqs)."""
    from gspread.utils import rowcol_to_a1

    sheet_data = []
    fmt_reqs   = []
    current_row = 1

    total_spend = sum(r['spend'] for r in all_ads)
    dl = datetime.strptime(date_str, '%Y-%m-%d').strftime('%d %b %Y').upper()

    def add_row(vals):
        nonlocal current_row
        sheet_data.append(vals)
        r = current_row; current_row += 1
        return r

    def add_blank(n=1):
        for _ in range(n): add_row([''])

    def fmt_req(r1, c1, r2, c2, **props):
        fmt_reqs.append({'repeatCell': {
            'range': {'sheetId': 0, 'startRowIndex': r1-1, 'endRowIndex': r2,
                      'startColumnIndex': c1-1, 'endColumnIndex': c2},
            'cell': {'userEnteredFormat': props},
            'fields': 'userEnteredFormat(' + ','.join(props.keys()) + ')',
        }})

    def merge(r1, c1, r2, c2):
        fmt_reqs.append({'mergeCells': {
            'range': {'sheetId': 0, 'startRowIndex': r1-1, 'endRowIndex': r2,
                      'startColumnIndex': c1-1, 'endColumnIndex': c2},
            'mergeType': 'MERGE_ALL'
        }})

    def section_hdr(title, ncols):
        r = add_row([title] + ['']*(ncols-1))
        merge(r, 1, r, ncols)
        fmt_req(r, 1, r, ncols,
                backgroundColor=SECTION_BG,
                textFormat={'bold': True, 'fontSize': 13, 'foregroundColor': WHITE},
                padding={'left': 10, 'top': 6, 'bottom': 6})
        return r

    def col_hdr(vals):
        r = add_row(vals)
        fmt_req(r, 1, r, len(vals),
                backgroundColor=COL_HDR_BG,
                textFormat={'bold': True, 'foregroundColor': WHITE},
                horizontalAlignment='CENTER',
                padding={'top': 4, 'bottom': 4})
        return r

    def type_badge(r, col, ctype):
        bg = TYPE_COLORS.get(ctype, rgb(127,140,141))
        fmt_req(r, col, r, col,
                backgroundColor=bg,
                textFormat={'bold': True, 'foregroundColor': WHITE},
                horizontalAlignment='CENTER')

    def total_row_fmt(r, ncols):
        fmt_req(r, 1, r, ncols, backgroundColor=TOTAL_BG,
                textFormat={'bold': True}, horizontalAlignment='RIGHT')
        fmt_req(r, 1, r, 1, horizontalAlignment='LEFT')

    def data_row_fmt(r, ncols, odd=False, is_other=False):
        bg = ORANGE_LIGHT if is_other else (ZEBRA_ODD if odd else ZEBRA_EVEN)
        fmt_req(r, 1, r, ncols, backgroundColor=bg, horizontalAlignment='RIGHT')
        fmt_req(r, 1, r, 2, horizontalAlignment='LEFT')

    # ══════════════════════════════════════════════════════════════════════════
    # SECTION 1 — MAIN CREATIVE REPORT
    # ══════════════════════════════════════════════════════════════════════════
    main_ads = [a for a in all_ads if a['creative_type'] != 'Testing']
    main_spend = sum(a['spend'] for a in main_ads)
    NCOLS1 = 11  # Type | Spend | Δ Spend | % Total | Ad Count | 1D ROAS | Δ 1D | 7D ROAS | Δ 7D | Avg/Ad | Δ Avg

    add_blank()
    section_hdr(f'🎨  CREATIVE PERFORMANCE  —  {dl}  (vs prev day)', NCOLS1)
    col_hdr(['Creative Type', 'Spend (₹)', 'Δ Spend', '% of Total', 'Ad Count',
             '1D ROAS', 'Δ 1D', '7D ROAS', 'Δ 7D', 'Avg ₹/Ad', 'Δ Avg ₹'])

    prev = prev_creative or {}

    for i, ctype in enumerate(CREATIVE_TYPES):
        rows = [a for a in main_ads if a['creative_type'] == ctype]
        if not rows: continue
        sp    = sum(a['spend'] for a in rows)
        count = len(rows)
        r1d   = w_roas(rows, '1d')
        r7d   = w_roas(rows, '7d')
        avg   = sp / count if count else 0

        p     = prev.get(ctype, {})
        p_sp  = p.get('spend')
        p_r1d = p.get('roas_1d')
        p_r7d = p.get('roas_7d')
        p_avg = (p_sp / p.get('count',1)) if p_sp and p.get('count') else None

        row_vals = [ctype,
                    fmt(sp),        fmt_delta_creative(sp, p_sp),
                    fmt_pct(sp, main_spend), count,
                    fmt_roas(r1d),  fmt_delta_creative(r1d, p_r1d, is_roas=True),
                    fmt_roas(r7d),  fmt_delta_creative(r7d, p_r7d, is_roas=True),
                    fmt(avg),       fmt_delta_creative(avg, p_avg)]
        r = add_row(row_vals)
        data_row_fmt(r, NCOLS1, odd=(i%2==1), is_other=(ctype=='Others'))
        type_badge(r, 1, ctype)
        fmt_req(r, 6, r, 9, backgroundColor=PURPLE_LIGHT)
        # Color deltas
        for col, curr, prev_val, hib in [(3,sp,p_sp,True),(7,r1d,p_r1d,True),(9,r7d,p_r7d,True)]:
            try:
                if curr and prev_val:
                    bg = delta_color_c(curr, prev_val, hib)
                    fmt_req(r, col, r, col, backgroundColor=bg,
                            textFormat={'foregroundColor': WHITE_C, 'bold': True})
            except: pass

    # Total
    r = add_row(['TOTAL',
                 fmt(main_spend), '',
                 '100%', len(main_ads),
                 fmt_roas(w_roas(main_ads, '1d')), '',
                 fmt_roas(w_roas(main_ads, '7d')), '',
                 fmt(main_spend/len(main_ads) if main_ads else 0), ''])
    total_row_fmt(r, NCOLS1)
    fmt_req(r, 6, r, 9, backgroundColor=PURPLE_LIGHT)

    add_blank(2)

    # ══════════════════════════════════════════════════════════════════════════
    # SECTION 2 — TESTING CREATIVE REPORT  (separate section)
    # ══════════════════════════════════════════════════════════════════════════
    test_ads  = [a for a in all_ads if a['creative_type'] == 'Testing']
    test_spend = sum(a['spend'] for a in test_ads)

    NCOLS2 = 10  # Type | Spend | % Test | Ad Count | 1D ROAS | 7D ROAS | Day 0 | Day 1-3 | Day 4-7 | Day 7+

    section_hdr(f'🧪  TESTING CREATIVES  —  {dl}  (₹{test_spend:,.0f} / {fmt_pct(test_spend, total_spend)} of total)', NCOLS2)
    col_hdr(['Creative Type', 'Spend (₹)', '% of Test', 'Ad Count',
             '1D ROAS', '7D ROAS', 'Day 0 ₹', 'Day 1–3 ₹', 'Day 4–7 ₹', 'Day 7+ ₹'])

    # Classify testing ads by creative sub-type
    def test_subtype(ad_name):
        n = ad_name.lower()
        if 'paras' in n:       return 'Paras'
        if 'partnership' in n: return 'Partnership'
        if any(k in n for k in ['catalog', 'catalogue']): return 'Catalogue'
        if 'static' in n:      return 'Static'
        return 'Motion'  # default for testing ads

    for i, ctype in enumerate(['Paras', 'Partnership', 'Motion', 'Static', 'Catalogue']):
        rows = [a for a in test_ads if test_subtype(a['ad_name']) == ctype]
        if not rows: continue
        sp    = sum(a['spend'] for a in rows)
        count = len(rows)
        r1d   = w_roas(rows, '1d')
        r7d   = w_roas(rows, '7d')

        # Day-wise spend
        day_spend = {b: sum(a['spend'] for a in rows if a['day_bucket'] == b)
                     for b in DAY_BUCKETS}

        row_vals = [ctype, fmt(sp), fmt_pct(sp, test_spend), count,
                    fmt_roas(r1d), fmt_roas(r7d),
                    fmt(day_spend['Day 0']), fmt(day_spend['Day 1–3']),
                    fmt(day_spend['Day 4–7']), fmt(day_spend['Day 7+'])]
        r = add_row(row_vals)
        data_row_fmt(r, NCOLS2, odd=(i%2==1))
        type_badge(r, 1, ctype)
        fmt_req(r, 5, r, 6, backgroundColor=PURPLE_LIGHT)
        # Tint day columns
        fmt_req(r, 7, r, 10, backgroundColor=rgb(232, 248, 245))

    # Testing total
    if test_ads:
        day_totals = {b: sum(a['spend'] for a in test_ads if a['day_bucket'] == b)
                      for b in DAY_BUCKETS}
        r = add_row(['TOTAL', fmt(test_spend), '100%', len(test_ads),
                     fmt_roas(w_roas(test_ads, '1d')), fmt_roas(w_roas(test_ads, '7d')),
                     fmt(day_totals['Day 0']), fmt(day_totals['Day 1–3']),
                     fmt(day_totals['Day 4–7']), fmt(day_totals['Day 7+'])])
        total_row_fmt(r, NCOLS2)
        fmt_req(r, 5, r, 6, backgroundColor=PURPLE_LIGHT)
        fmt_req(r, 7, r, 10, backgroundColor=rgb(209, 247, 235))

    add_blank(2)

    # ══════════════════════════════════════════════════════════════════════════
    # SECTION 3 — COMBINED SUMMARY (Main + Testing side by side)
    # ══════════════════════════════════════════════════════════════════════════
    NCOLS3 = 5
    section_hdr(f'📊  OVERALL SPEND SPLIT  —  {dl}', NCOLS3)
    col_hdr(['Bucket', 'Spend (₹)', '% of Total', 'Ad Count', '7D ROAS'])

    buckets = [
        ('Main Creatives', main_ads),
        ('Testing',        test_ads),
    ]
    for i, (label, rows) in enumerate(buckets):
        sp  = sum(a['spend'] for a in rows)
        r   = add_row([label, fmt(sp), fmt_pct(sp, total_spend),
                       len(rows), fmt_roas(w_roas(rows, '7d'))])
        data_row_fmt(r, NCOLS3, odd=(i%2==1))
        bg = TYPE_COLORS.get('Testing' if label == 'Testing' else 'Motion', rgb(100,100,100))
        fmt_req(r, 1, r, 1, backgroundColor=bg,
                textFormat={'bold': True, 'foregroundColor': WHITE})

    r = add_row(['GRAND TOTAL', fmt(total_spend), '100%', len(all_ads),
                 fmt_roas(w_roas(all_ads, '7d'))])
    total_row_fmt(r, NCOLS3)

    add_blank(2)

    # ══════════════════════════════════════════════════════════════════════════
    # SECTION 4 — TESTING AD DETAIL (individual ads, for review)
    # ══════════════════════════════════════════════════════════════════════════
    NCOLS4 = 8
    section_hdr(f'🔬  TESTING AD DETAIL  —  {dl}  ({len(test_ads)} ads)', NCOLS4)
    col_hdr(['Ad Name', 'Campaign', 'Spend (₹)', '% of Test',
             '1D ROAS', '7D ROAS', 'Age', 'Sub-type'])

    for i, a in enumerate(sorted(test_ads, key=lambda x: -x['spend'])):
        r = add_row([
            a['ad_name'],
            a['campaign_name'][:50],
            fmt(a['spend']),
            fmt_pct(a['spend'], test_spend),
            fmt_roas(a['roas_1d']),
            fmt_roas(a['roas_7d']),
            a['day_bucket'],
            test_subtype(a['ad_name']),
        ])
        data_row_fmt(r, NCOLS4, odd=(i%2==1))
        fmt_req(r, 5, r, 6, backgroundColor=PURPLE_LIGHT)
        # Color age bucket
        age_colors = {
            'Day 0':   rgb(39, 174, 96),
            'Day 1–3': rgb(41, 128, 185),
            'Day 4–7': rgb(243, 156, 18),
            'Day 7+':  rgb(231, 76, 60),
        }
        age_bg = age_colors.get(a['day_bucket'], rgb(200,200,200))
        fmt_req(r, 7, r, 7, backgroundColor=age_bg,
                textFormat={'foregroundColor': WHITE, 'bold': True},
                horizontalAlignment='CENTER')

    return sheet_data, fmt_reqs


# ── Write tab ─────────────────────────────────────────────────────────────────
def write_tab(sh, date_str, sheet_data, fmt_reqs):
    d = datetime.strptime(date_str, '%Y-%m-%d')
    tab_name = f"📊 Creative Report {d.day} {d.strftime('%b').upper()} {d.strftime('%y')}"

    existing = [ws.title for ws in sh.worksheets()]
    max_cols = max((len(r) for r in sheet_data), default=10)

    if tab_name in existing:
        ws = sh.worksheet(tab_name)
        sh.del_worksheet(ws)

    ws = sh.add_worksheet(title=tab_name, rows=max(len(sheet_data)+20, 300), cols=max_cols+2)
    all_ws = sh.worksheets()
    sh.reorder_worksheets([ws] + [w for w in all_ws if w.title != tab_name])

    # Patch sheetId
    for req in fmt_reqs:
        for key in ['repeatCell', 'mergeCells']:
            if key in req and 'range' in req[key]:
                req[key]['range']['sheetId'] = ws.id

    # Pad rows
    for row in sheet_data:
        while len(row) < max_cols: row.append('')

    ws.update(values=sheet_data, range_name='A1', value_input_option='USER_ENTERED')
    if fmt_reqs:
        sh.batch_update({'requests': fmt_reqs})

    # Column widths
    sh.batch_update({'requests': [
        {'updateDimensionProperties': {
            'range': {'sheetId': ws.id, 'dimension': 'COLUMNS', 'startIndex': 0, 'endIndex': 1},
            'properties': {'pixelSize': 280}, 'fields': 'pixelSize'}},
        {'updateDimensionProperties': {
            'range': {'sheetId': ws.id, 'dimension': 'COLUMNS', 'startIndex': 1, 'endIndex': 2},
            'properties': {'pixelSize': 240}, 'fields': 'pixelSize'}},
        {'updateDimensionProperties': {
            'range': {'sheetId': ws.id, 'dimension': 'COLUMNS', 'startIndex': 2, 'endIndex': max_cols},
            'properties': {'pixelSize': 110}, 'fields': 'pixelSize'}},
    ]})

    print(f"  ✅ '{tab_name}' — {len(sheet_data)} rows")
    return tab_name


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--date', default=None)
    parser.add_argument('--accounts', default=None, help='Comma-separated account keys, default=all')
    args = parser.parse_args()

    date_str = args.date or (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
    accounts = args.accounts.split(',') if args.accounts else list(ACCOUNT_MAP.keys())

    print(f"\n🚀 Creative Report — {date_str}")
    print(f"   Accounts: {accounts}")

    creds = Credentials.from_service_account_file(SA_FILE, scopes=SCOPES)
    gc    = gspread.authorize(creds)
    sh    = gc.open_by_key(SHEET_ID)

    all_ads = []
    for acct_key in accounts:
        if acct_key not in ACCOUNT_MAP:
            print(f"  ⚠️  Unknown account: {acct_key}")
            continue
        act_id, portal = ACCOUNT_MAP[acct_key]
        if not act_id:
            print(f"  ⚠️  No act_id for {acct_key}")
            continue
        try:
            ads = fetch_ads(act_id, date_str)
            for a in ads:
                a['portal'] = portal
                a['account_key'] = acct_key
            all_ads.extend(ads)
            print(f"  ✅ {acct_key}: {len(ads)} ads")
        except Exception as e:
            print(f"  ❌ {acct_key}: {e}")

    if not all_ads:
        print("❌ No ad data found.")
        return

    total_spend = sum(a['spend'] for a in all_ads)
    print(f"\n  Total ads: {len(all_ads)} | Total spend: ₹{total_spend:,.0f}")

    print("\n📥 Loading previous day creative data...")
    prev_creative = load_prev_creative(sh, date_str)

    print("\n📊 Building report...")
    sheet_data, fmt_reqs = build_report(all_ads, date_str, prev_creative)

    print("✍️  Writing to sheet...")
    tab_name = write_tab(sh, date_str, sheet_data, fmt_reqs)

    print(f"\n✅ Done: '{tab_name}'")
    print(f"🔗 https://docs.google.com/spreadsheets/d/{SHEET_ID}")


if __name__ == '__main__':
    main()
