"""
Campaign Tracker Reports — Single consolidated tab with all 3 reports + formatting
Tab name: 📊 Reports [DATE]

Sections (stacked vertically):
  1. Category Budget    — Portal × Category matrix
  2. Audience Budget    — Category × Audience per portal
  3. Product Budget     — Product × Audience per portal + Unmapped column

Usage:
  python3 campaign_tracker_reports.py --date 2026-04-19
"""

import os, sys, argparse
import gspread
from gspread.utils import rowcol_to_a1
from google.oauth2.service_account import Credentials
from datetime import datetime, timedelta
from collections import defaultdict

SA_FILE  = '/Users/pulkitsharma/.openclaw/workspace/google-service-account.json'
SHEET_ID = '11IAPsJlil75aehYf5IzpSaTCLcAgPk9-57p6ZuPNNQM'

SCOPES = ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive']

PORTALS = ['SM', 'SML', 'NBP']

CATEGORY_ORDER = [
    'Skin', 'Hair', 'Crystal', 'Jewellery',
    'Nutraceuticals', 'Fragrance', 'Clocks', 'Frames', 'Mix', 'Unmapped'
]

AUDIENCE_ORDER = [
    'Lifetime audience', 'Loose', 'Sale 30',
    'Visitor 180 Day', 'Visitor 30 Day',
    'Impression 30 retarget', 'Impression 7 retarget',
    'C1_C4 _Meta', 'DS',
    'ADD to cart 7 days', '30 days add to cart',
    'Lookalike (IN, 1%) - prepaid purchase',
    'NTN | Recent 0-30D', 'NTN | Recent 31-60D',
    'NTN | Repeat Buyers 2+', 'NTN | Loyal Buyers 3+',
    'NTN | SM Only Buyers',
    'NTN | Crystal Buyers — 30 to 180 Days',
    'NTN | Crystal Buyers — 365+ Days (Lapsed)',
    'NTN | Product: Xtreme Hair (1+)',
    'NTN | Lip Bright + Pit Glow Buyers',
    'Unmapped'
]

# ── Color palette ─────────────────────────────────────────────────────────────
def rgb(r, g, b):
    return {'red': r/255, 'green': g/255, 'blue': b/255}

COLORS = {
    'section_header': rgb(30, 30, 30),       # near-black bg
    'section_text':   rgb(255, 255, 255),    # white text
    'col_header':     rgb(52, 73, 94),       # dark navy
    'col_text':       rgb(255, 255, 255),    # white text
    'portal_SM':      rgb(41, 128, 185),     # blue
    'portal_SML':     rgb(39, 174, 96),      # green
    'portal_NBP':     rgb(192, 57, 43),      # red
    'total_row':      rgb(236, 240, 241),    # light grey
    'subtotal':       rgb(248, 249, 250),    # near-white
    'zebra_even':     rgb(255, 255, 255),    # white
    'zebra_odd':      rgb(245, 247, 250),    # very light blue
    'unmapped':       rgb(253, 245, 230),    # light orange
}

PORTAL_COLORS = {
    'SM':  rgb(41, 128, 185),
    'SML': rgb(39, 174, 96),
    'NBP': rgb(192, 57, 43),
}

# ── Helpers ───────────────────────────────────────────────────────────────────
def safe_budget(val):
    try:
        return float(str(val).replace(',', '').strip()) if val else 0.0
    except:
        return 0.0

def fmt(val):
    if val == 0 or val == '':
        return ''
    return f"{float(val):,.0f}"

def weighted_roas(row_list, rev_key):
    """
    Weighted ROAS = sum(revenue_Nd) / sum(spend_Nd) using actual per-window spend.
    rev_key: 'rev_1d' | 'rev_2d' | 'rev_3d' | 'rev_7d'
    spend_key auto-derived: 'sp_1d' etc.
    """
    sp_key = rev_key.replace('rev_', 'sp_')
    total_rev   = sum(r[rev_key]  for r in row_list if r.get(rev_key)  is not None)
    total_spend = sum(r.get(sp_key, r['spend']) for r in row_list if r.get(rev_key) is not None)
    if total_spend == 0:
        return ''
    return round(total_rev / total_spend, 2)

def fmt_roas(val):
    if val == '' or val is None:
        return ''
    return f"{val:.2f}x"

def fmt_delta(curr, prev, is_roas=False, is_pct=False):
    """Format day-over-day delta with ▲/▼ arrow."""
    try:
        c = float(curr) if curr not in ('', None) else None
        p = float(prev) if prev not in ('', None) else None
        if c is None or p is None:
            return ''
        delta = c - p
        if delta == 0:
            return '—'
        arrow = '▲' if delta > 0 else '▼'
        if is_roas:
            return f"{arrow} {abs(delta):.2f}x"
        if is_pct:
            return f"{arrow} {abs(delta):.1f}%"
        return f"{arrow} {abs(delta):,.0f}"
    except:
        return ''

def delta_color(curr, prev, higher_is_better=True):
    """Returns green/red/grey based on direction."""
    try:
        c = float(curr) if curr not in ('', None) else None
        p = float(prev) if prev not in ('', None) else None
        if c is None or p is None:
            return rgb(200, 200, 200)
        if c > p:
            return rgb(39, 174, 96) if higher_is_better else rgb(231, 76, 60)
        if c < p:
            return rgb(231, 76, 60) if higher_is_better else rgb(39, 174, 96)
        return rgb(200, 200, 200)
    except:
        return rgb(200, 200, 200)

def date_label(date_str):
    d = datetime.strptime(date_str, '%Y-%m-%d')
    return f"{d.day} {d.strftime('%b').upper()} {d.strftime('%y')}"

def a1(row, col):
    return rowcol_to_a1(row, col)

def range_str(r1, c1, r2, c2):
    return f"{a1(r1,c1)}:{a1(r2,c2)}"

# ── Load tracker data ─────────────────────────────────────────────────────────
def load_tracker_data(sh, date_str):
    import glob, json as _json, os as _os

    all_rows = []
    dl = date_label(date_str)

    # Account ID → portal map (from .env)
    from dotenv import dotenv_values
    env = dotenv_values('/Users/pulkitsharma/.openclaw/workspace/.env')

    # Load all revenue caches for this date
    revenue_cache = {}  # campaign_id → {spend_1d, rev_1d, spend_2d, rev_2d, ...}
    cache_files = glob.glob(f'/tmp/tracker_cache_act_*_{date_str}.json')
    for cf in cache_files:
        try:
            with open(cf) as f:
                data = _json.load(f)
            revenue_cache.update(data)
        except:
            pass
    print(f"  📦 Revenue cache: {len(revenue_cache)} campaigns from {len(cache_files)} files")

    for portal in PORTALS:
        tab = f"{portal} {dl}"
        try:
            ws = sh.worksheet(tab)
            data = ws.get_all_values()
            if len(data) < 2:
                print(f"  ⚠️  Tab '{tab}' empty")
                continue
            loaded = 0
            seen_cids = set()  # deduplicate by campaign_id within this tab
            for row in data[1:]:
                if not row[0]:
                    continue
                budget = safe_budget(row[11])
                if budget == 0:
                    continue

                spend   = safe_budget(row[6])
                cid     = row[19].strip() if len(row) > 19 else ''

                # Skip duplicate CIDs — take first row only
                if cid and cid in seen_cids:
                    continue
                if cid:
                    seen_cids.add(cid)
                cache   = revenue_cache.get(cid, {})

                # Use actual spend + revenue from cache for accurate ROAS
                # Fallback: sheet ROAS × spend if cache missing
                def get_rev(days_key, sheet_roas_col):
                    rev = cache.get(f'rev_{days_key}d')
                    sp  = cache.get(f'spend_{days_key}d', spend)
                    if rev is not None:
                        return rev, sp
                    # Fallback to sheet ROAS
                    try:
                        v = float(row[sheet_roas_col]) if len(row) > sheet_roas_col and row[sheet_roas_col] not in ('-','') else None
                        return (v * spend, spend) if v is not None else (None, spend)
                    except:
                        return None, spend

                rev_1d, sp_1d = get_rev(1, 7)
                rev_2d, sp_2d = get_rev(2, 8)
                rev_3d, sp_3d = get_rev(3, 9)
                rev_7d, sp_7d = get_rev(7, 10)

                category = (row[25].strip() if len(row) > 25 else '') or 'Unmapped'
                audience = (row[23].strip() if len(row) > 23 else '') or 'Unmapped'
                product  = (row[24].strip() if len(row) > 24 else '') or 'Unmapped'

                all_rows.append({
                    'portal':   portal,
                    'name':     row[1].strip(),
                    'status':   row[3].strip(),
                    'campaign_id': cid,
                    'budget':   budget,
                    'spend':    spend,
                    'type':     row[22].strip() if len(row) > 22 else '',
                    'category': category,
                    'audience': audience,
                    'product':  product,
                    # Actual revenue per window (for correct weighted ROAS)
                    'rev_1d': rev_1d, 'sp_1d': sp_1d,
                    'rev_2d': rev_2d, 'sp_2d': sp_2d,
                    'rev_3d': rev_3d, 'sp_3d': sp_3d,
                    'rev_7d': rev_7d, 'sp_7d': sp_7d,
                    # For unmapped list
                    'unmapped_reason': (
                        ('Category ' if category == 'Unmapped' else '') +
                        ('Audience ' if audience == 'Unmapped' else '') +
                        ('Product '  if product  == 'Unmapped' else '')
                    ).strip(),
                })
                loaded += 1
            print(f"  ✅ {tab}: {loaded} rows")
        except gspread.WorksheetNotFound:
            print(f"  ⚠️  Tab '{tab}' not found")
    return all_rows

# ── Build all sheet data + format requests ─────────────────────────────────────
def build_full_report(rows, date_str, prev_map=None):
    """
    Returns (sheet_data, format_requests) where:
    - sheet_data: list of rows (list of values) for the entire tab
    - format_requests: list of Sheets API batchUpdate requests
    """
    sheet_data = []
    fmt_reqs = []
    current_row = 1  # 1-indexed for Sheets API

    all_cats = sorted(set(r['category'] for r in rows))
    cats = [c for c in CATEGORY_ORDER if c in all_cats] + [c for c in all_cats if c not in CATEGORY_ORDER]

    all_auds = sorted(set(r['audience'] for r in rows))
    auds = [a for a in AUDIENCE_ORDER if a in all_auds] + [a for a in all_auds if a not in AUDIENCE_ORDER]

    def add_row(values, note=None):
        nonlocal current_row
        sheet_data.append(values)
        r = current_row
        current_row += 1
        return r

    def add_blank(n=1):
        for _ in range(n):
            add_row([''])

    def fmt_req(r1, c1, r2, c2, **props):
        req = {
            'repeatCell': {
                'range': {
                    'sheetId': 0,
                    'startRowIndex': r1 - 1,
                    'endRowIndex': r2,
                    'startColumnIndex': c1 - 1,
                    'endColumnIndex': c2,
                },
                'cell': {'userEnteredFormat': props},
                'fields': 'userEnteredFormat(' + ','.join(props.keys()) + ')',
            }
        }
        fmt_reqs.append(req)

    def merge_req(r1, c1, r2, c2):
        fmt_reqs.append({
            'mergeCells': {
                'range': {
                    'sheetId': 0,
                    'startRowIndex': r1 - 1,
                    'endRowIndex': r2,
                    'startColumnIndex': c1 - 1,
                    'endColumnIndex': c2,
                },
                'mergeType': 'MERGE_ALL'
            }
        })

    def section_header(title, num_cols):
        nonlocal current_row
        r = add_row([title] + [''] * (num_cols - 1))
        merge_req(r, 1, r, num_cols)
        fmt_req(r, 1, r, num_cols,
                backgroundColor=COLORS['section_header'],
                textFormat={'bold': True, 'fontSize': 13, 'foregroundColor': COLORS['section_text']},
                horizontalAlignment='LEFT',
                padding={'top': 6, 'bottom': 6, 'left': 10})
        return r

    def col_header_row(values):
        nonlocal current_row
        r = add_row(values)
        fmt_req(r, 1, r, len(values),
                backgroundColor=COLORS['col_header'],
                textFormat={'bold': True, 'foregroundColor': COLORS['col_text']},
                horizontalAlignment='CENTER',
                padding={'top': 4, 'bottom': 4})
        return r

    def portal_subheader(portal, num_cols):
        nonlocal current_row
        r = add_row([f'  {portal}'] + [''] * (num_cols - 1))
        merge_req(r, 1, r, num_cols)
        color = PORTAL_COLORS.get(portal, rgb(100, 100, 100))
        fmt_req(r, 1, r, num_cols,
                backgroundColor=color,
                textFormat={'bold': True, 'fontSize': 11, 'foregroundColor': COLORS['section_text']},
                padding={'top': 4, 'bottom': 4, 'left': 8})
        return r

    def total_row_fmt(r, num_cols):
        fmt_req(r, 1, r, num_cols,
                backgroundColor=COLORS['total_row'],
                textFormat={'bold': True},
                horizontalAlignment='RIGHT')

    def data_row_fmt(r, num_cols, is_odd=False, is_unmapped=False):
        bg = COLORS['unmapped'] if is_unmapped else (COLORS['zebra_odd'] if is_odd else COLORS['zebra_even'])
        fmt_req(r, 1, r, 1, horizontalAlignment='LEFT')
        fmt_req(r, 2, r, num_cols, horizontalAlignment='RIGHT', backgroundColor=bg)
        fmt_req(r, 1, r, 1, backgroundColor=bg)

    # ══════════════════════════════════════════════════════════════════════════
    # SECTION 1 — CATEGORY BUDGET  (Sales / Retarget split + DoD delta)
    # Cols: Cat | Type | Total Bud | Δ Bud | Camps | Δ Camps | 1D ROAS | Δ1D | 7D ROAS | Δ7D
    # ══════════════════════════════════════════════════════════════════════════
    NCOLS_S1 = 12

    add_blank()
    section_header(f'📦  CATEGORY BUDGET  —  {date_label(date_str)}  (vs prev day)', NCOLS_S1)

    grp = add_row(['Category', 'Type', 'Budget', 'Δ Budget', 'Camps', 'Δ Camps',
                   '1D ROAS', 'Δ 1D', '7D ROAS', 'Δ 7D', 'Spend', 'Δ Spend'])
    fmt_req(grp, 1, grp, NCOLS_S1,
            backgroundColor=COLORS['col_header'],
            textFormat={'bold': True, 'foregroundColor': COLORS['col_text']},
            horizontalAlignment='CENTER')
    fmt_req(grp, 7, grp, 10, backgroundColor=rgb(142, 68, 173))
    fmt_req(grp, 4, grp, 4,  backgroundColor=rgb(80, 80, 80))
    fmt_req(grp, 6, grp, 6,  backgroundColor=rgb(80, 80, 80))
    fmt_req(grp, 8, grp, 8,  backgroundColor=rgb(80, 80, 80))
    fmt_req(grp, 10, grp, 10,backgroundColor=rgb(80, 80, 80))
    fmt_req(grp, 12, grp, 12,backgroundColor=rgb(80, 80, 80))

    def p_get(cat, typ, metric):
        """Get prev day value for (cat, type) key."""
        if not prev_map: return None
        v = prev_map.get(('cat_type', (cat, typ)), {})
        return v.get(metric)

    # Aggregate current
    cat_type_data = defaultdict(lambda: defaultdict(lambda: defaultdict(lambda: {'budget': 0, 'count': 0})))
    cat_type_rows = defaultdict(lambda: defaultdict(list))
    for r in rows:
        cat_type_data[r['category']][r['type']][r['portal']]['budget'] += r['budget']
        cat_type_data[r['category']][r['type']][r['portal']]['count']  += 1
        cat_type_rows[r['category']][r['type']].append(r)

    row_idx = 0

    for cat in cats:
        if cat not in cat_type_data:
            continue

        for ti, typ in enumerate(['Sales', 'Retarget']):
            if typ not in cat_type_data[cat]:
                continue

            row_total = sum(cat_type_data[cat][typ][p]['budget'] for p in PORTALS)
            row_camps = sum(cat_type_data[cat][typ][p]['count']  for p in PORTALS)
            tr        = cat_type_rows[cat][typ]
            spend_cur = sum(r['spend'] for r in tr)
            r1d       = weighted_roas(tr, 'rev_1d')
            r7d       = weighted_roas(tr, 'rev_7d')

            # Prev day values
            p_bud   = p_get(cat, typ, 'budget')
            p_camps = p_get(cat, typ, 'camps')
            p_spend = p_get(cat, typ, 'spend')
            p_r1d   = p_get(cat, typ, 'roas_1d')
            p_r7d   = p_get(cat, typ, 'roas_7d')

            row_vals = [
                cat if ti == 0 else '', typ,
                fmt(row_total), fmt_delta(row_total, p_bud),
                row_camps, fmt_delta(row_camps, p_camps),
                fmt_roas(r1d), fmt_delta(r1d, p_r1d, is_roas=True),
                fmt_roas(r7d), fmt_delta(r7d, p_r7d, is_roas=True),
                fmt(spend_cur), fmt_delta(spend_cur, p_spend),
            ]
            r = add_row(row_vals)
            is_odd = (row_idx % 2 == 1)
            data_row_fmt(r, NCOLS_S1, is_odd=is_odd, is_unmapped=(cat in ('Unmapped','Mix')))
            fmt_req(r, 7, r, 10, backgroundColor=rgb(243, 232, 255))

            # Type badge
            type_bg = rgb(41, 128, 185) if typ == 'Sales' else rgb(39, 174, 96)
            fmt_req(r, 2, r, 2, backgroundColor=type_bg,
                    textFormat={'foregroundColor': COLORS['col_text'], 'bold': True},
                    horizontalAlignment='CENTER')

            # Delta coloring
            for col_idx, curr_val, prev_val, higher_good in [
                (4,  row_total, p_bud,   True),
                (8,  r1d,       p_r1d,   True),
                (10, r7d,       p_r7d,   True),
                (12, spend_cur, p_spend, True),
            ]:
                try:
                    if curr_val and prev_val:
                        bg = delta_color(curr_val, prev_val, higher_is_better=higher_good)
                        fmt_req(r, col_idx, r, col_idx, backgroundColor=bg,
                                textFormat={'foregroundColor': WHITE, 'bold': True})
                except:
                    pass

            row_idx += 1

        # Category subtotal
        all_cat_rows = cat_type_rows[cat].get('Sales',[]) + cat_type_rows[cat].get('Retarget',[])
        sub_bud   = sum(cat_type_data[cat][t][p]['budget'] for t in ['Sales','Retarget'] if t in cat_type_data[cat] for p in PORTALS)
        sub_camps = sum(cat_type_data[cat][t][p]['count']  for t in ['Sales','Retarget'] if t in cat_type_data[cat] for p in PORTALS)
        sub_spend = sum(r['spend'] for r in all_cat_rows)
        pb_bud    = sum(p_get(cat,t,'budget') or 0 for t in ['Sales','Retarget'])
        pb_spend  = sum(p_get(cat,t,'spend')  or 0 for t in ['Sales','Retarget'])
        sub_r1d   = weighted_roas(all_cat_rows, 'rev_1d')
        sub_r7d   = weighted_roas(all_cat_rows, 'rev_7d')
        pb_r1d    = next((p_get(cat,t,'roas_1d') for t in ['Sales','Retarget'] if p_get(cat,t,'roas_1d')), None)
        pb_r7d    = next((p_get(cat,t,'roas_7d') for t in ['Sales','Retarget'] if p_get(cat,t,'roas_7d')), None)

        sub = [f'  ↳ {cat} Total', '',
               fmt(sub_bud), fmt_delta(sub_bud, pb_bud),
               sub_camps, '',
               fmt_roas(sub_r1d), fmt_delta(sub_r1d, pb_r1d, is_roas=True),
               fmt_roas(sub_r7d), fmt_delta(sub_r7d, pb_r7d, is_roas=True),
               fmt(sub_spend), fmt_delta(sub_spend, pb_spend)]
        r = add_row(sub)
        fmt_req(r, 1, r, NCOLS_S1, backgroundColor=COLORS['subtotal'],
                textFormat={'bold': True, 'italic': True})
        fmt_req(r, 7, r, 10, backgroundColor=rgb(230, 210, 255))
        add_blank()
        row_idx += 1

    # Grand total
    gt_bud   = sum(cat_type_data[cat][t][p]['budget'] for cat in cats for t in ['Sales','Retarget'] if cat in cat_type_data and t in cat_type_data[cat] for p in PORTALS)
    gt_camps = sum(cat_type_data[cat][t][p]['count']  for cat in cats for t in ['Sales','Retarget'] if cat in cat_type_data and t in cat_type_data[cat] for p in PORTALS)
    gt_spend = sum(r['spend'] for r in rows)
    pb_gt_bud   = sum(prev_map.get(('cat_type', k), {}).get('budget',0) for k in prev_map if k[0]=='cat_type')
    pb_gt_spend = sum(prev_map.get(('cat_type', k), {}).get('spend', 0) for k in prev_map if k[0]=='cat_type')
    total_vals = ['GRAND TOTAL', '',
                  fmt(gt_bud),   fmt_delta(gt_bud, pb_gt_bud),
                  gt_camps, '',
                  fmt_roas(weighted_roas(rows,'rev_1d')), '',
                  fmt_roas(weighted_roas(rows,'rev_7d')), '',
                  fmt(gt_spend), fmt_delta(gt_spend, pb_gt_spend)]
    r = add_row(total_vals)
    total_row_fmt(r, NCOLS_S1)

    add_blank(2)

    # ══════════════════════════════════════════════════════════════════════════
    # SECTION 2 — AUDIENCE BUDGET (per portal, categories as columns + Δ Spend + Δ 7D ROAS)
    # ══════════════════════════════════════════════════════════════════════════
    NCOLS_S2 = len(cats) + 8  # Audience + cats + TOTAL + 4 ROAS + Δ Spend + Δ 7D

    section_header(f'🎯  AUDIENCE BUDGET  —  {date_label(date_str)}  (vs prev day)', NCOLS_S2)

    for portal in PORTALS:
        portal_rows = [r for r in rows if r['portal'] == portal]
        if not portal_rows:
            continue

        portal_subheader(portal, NCOLS_S2)
        col_header_row(['Audience'] + cats + ['TOTAL', '1D ROAS', '2D ROAS', '3D ROAS', '7D ROAS', 'Δ Budget', 'Δ 7D ROAS'])

        aud_data     = defaultdict(lambda: defaultdict(float))
        aud_rows_map = defaultdict(list)
        for r in portal_rows:
            aud_data[r['audience']][r['category']] += r['budget']
            aud_rows_map[r['audience']].append(r)

        portal_auds    = [a for a in auds if a in aud_data]
        aud_cat_totals = defaultdict(float)

        for i, aud in enumerate(portal_auds):
            row_vals  = [aud]
            row_total = 0
            for cat in cats:
                b = aud_data[aud].get(cat, 0)
                row_vals.append(fmt(b))
                row_total += b
                aud_cat_totals[cat] += b
            ar    = aud_rows_map[aud]
            r7d   = weighted_roas(ar, 'rev_7d')
            # Prev values
            p_key = ('aud_cat', (portal, aud, cats[0])) if cats else None  # approximate
            p_bud = sum(prev_map.get(('aud_cat', (portal, aud, c)), {}).get('budget', 0) for c in cats) if prev_map else None
            p_bud = p_bud if p_bud else None
            p_r7d = None
            if prev_map:
                prev_aud_rows = [v for k,v in prev_map.items() if k[0]=='aud_cat' and k[1][0]==portal and k[1][1]==aud]
                if prev_aud_rows:
                    pr = sum(v.get('rev_7d',0) for v in prev_aud_rows)
                    ps = sum(v.get('sp_7d', v.get('spend',0)) for v in prev_aud_rows)
                    p_r7d = round(pr/ps, 2) if ps else None

            row_vals.append(fmt(row_total))
            row_vals += [fmt_roas(weighted_roas(ar, 'rev_1d')),
                         fmt_roas(weighted_roas(ar, 'rev_2d')),
                         fmt_roas(weighted_roas(ar, 'rev_3d')),
                         fmt_roas(r7d),
                         fmt_delta(row_total, p_bud),
                         fmt_delta(r7d, p_r7d, is_roas=True)]
            r = add_row(row_vals)
            data_row_fmt(r, NCOLS_S2, is_odd=(i % 2 == 1), is_unmapped=(aud == 'Unmapped'))
            roas_start = len(cats) + 3
            fmt_req(r, roas_start, r, roas_start+3, backgroundColor=rgb(243, 232, 255))
            # Delta coloring
            delta_col = NCOLS_S2
            try:
                if r7d and p_r7d:
                    fmt_req(r, delta_col, r, delta_col,
                            backgroundColor=delta_color(r7d, p_r7d),
                            textFormat={'foregroundColor': WHITE, 'bold': True})
            except:
                pass

        # Subtotal
        sub_vals  = ['TOTAL']
        sub_grand = 0
        for cat in cats:
            t = aud_cat_totals.get(cat, 0)
            sub_vals.append(fmt(t))
            sub_grand += t
        sub_vals.append(fmt(sub_grand))
        sub_vals += [fmt_roas(weighted_roas(portal_rows, 'rev_1d')),
                     fmt_roas(weighted_roas(portal_rows, 'rev_2d')),
                     fmt_roas(weighted_roas(portal_rows, 'rev_3d')),
                     fmt_roas(weighted_roas(portal_rows, 'rev_7d')),
                     '', '']
        r = add_row(sub_vals)
        total_row_fmt(r, NCOLS_S2)
        add_blank()

    add_blank()

    # ══════════════════════════════════════════════════════════════════════════
    # SECTION 3 — PRODUCT BUDGET (per portal, audiences as columns)
    # ══════════════════════════════════════════════════════════════════════════
    NCOLS_S3 = len(auds) + 9  # Category + Product + auds + TOTAL + 4 ROAS + Δ Budget + Δ 7D

    section_header(f'🛍️  PRODUCT BUDGET  —  {date_label(date_str)}', NCOLS_S3)

    for portal in PORTALS:
        portal_rows = [r for r in rows if r['portal'] == portal]
        if not portal_rows:
            continue

        portal_subheader(portal, NCOLS_S3)
        col_header_row(['Category', 'Product'] + auds + ['TOTAL', '1D ROAS', '2D ROAS', '3D ROAS', '7D ROAS', 'Δ Budget', 'Δ 7D ROAS'])

        prod_data     = defaultdict(lambda: defaultdict(lambda: defaultdict(float)))
        prod_rows_map = defaultdict(lambda: defaultdict(list))
        for r in portal_rows:
            prod_data[r['category']][r['product']][r['audience']] += r['budget']
            prod_rows_map[r['category']][r['product']].append(r)

        row_idx = 0
        for cat in cats:
            if cat not in prod_data:
                continue
            products       = sorted(prod_data[cat].keys())
            cat_aud_totals = defaultdict(float)
            cat_all_rows   = []

            for pi, prod in enumerate(products):
                row_vals  = [cat if pi == 0 else '', prod]
                row_total = 0
                for aud in auds:
                    b = prod_data[cat][prod].get(aud, 0)
                    row_vals.append(fmt(b))
                    row_total += b
                    cat_aud_totals[aud] += b
                row_vals.append(fmt(row_total))
                pr = prod_rows_map[cat][prod]
                cat_all_rows.extend(pr)
                r7d = weighted_roas(pr, 'rev_7d')
                # Prev
                p_prod = prev_map.get(('prod_aud', (portal, cat, prod)), {}) if prev_map else {}
                p_bud  = p_prod.get('budget')
                p_r7d_prod = p_prod.get('roas_7d')

                row_vals += [fmt_roas(weighted_roas(pr, 'rev_1d')),
                             fmt_roas(weighted_roas(pr, 'rev_2d')),
                             fmt_roas(weighted_roas(pr, 'rev_3d')),
                             fmt_roas(r7d),
                             fmt_delta(row_total, p_bud),
                             fmt_delta(r7d, p_r7d_prod, is_roas=True)]
                r = add_row(row_vals)
                is_unmapped = (prod == 'Unmapped')
                data_row_fmt(r, NCOLS_S3, is_odd=(row_idx % 2 == 1), is_unmapped=is_unmapped)
                roas_col = len(auds) + 4
                fmt_req(r, roas_col, r, roas_col+3, backgroundColor=rgb(243, 232, 255))
                # Delta coloring
                try:
                    if r7d and p_r7d_prod:
                        fmt_req(r, NCOLS_S3, r, NCOLS_S3,
                                backgroundColor=delta_color(r7d, p_r7d_prod),
                                textFormat={'foregroundColor': WHITE, 'bold': True})
                except:
                    pass
                row_idx += 1

            # Category subtotal
            sub_vals  = ['', f'↳ {cat} Total']
            sub_total = 0
            for aud in auds:
                t = cat_aud_totals.get(aud, 0)
                sub_vals.append(fmt(t))
                sub_total += t
            sub_r7d = weighted_roas(cat_all_rows, 'rev_7d')
            p_cat   = prev_map.get(('prod_aud', (portal, cat, 'Mix/Multiple')), {}) if prev_map else {}
            sub_vals.append(fmt(sub_total))
            sub_vals += [fmt_roas(weighted_roas(cat_all_rows, 'rev_1d')),
                         fmt_roas(weighted_roas(cat_all_rows, 'rev_2d')),
                         fmt_roas(weighted_roas(cat_all_rows, 'rev_3d')),
                         fmt_roas(sub_r7d), '', '']
            r = add_row(sub_vals)
            fmt_req(r, 1, r, NCOLS_S3,
                    backgroundColor=COLORS['subtotal'],
                    textFormat={'bold': True, 'italic': True},
                    horizontalAlignment='RIGHT')
            fmt_req(r, 1, r, 2, horizontalAlignment='LEFT')
            fmt_req(r, len(auds)+4, r, len(auds)+7, backgroundColor=rgb(230, 210, 255))

        add_blank()

    # ══════════════════════════════════════════════════════════════════════════
    # SECTION 4 — UNMAPPED CAMPAIGNS (for cleanup)
    # ══════════════════════════════════════════════════════════════════════════
    unmapped = [r for r in rows if r['category'] == 'Unmapped' or r['audience'] == 'Unmapped' or r['product'] == 'Unmapped']

    NCOLS_S4 = 12
    section_header(f'⚠️  UNMAPPED CAMPAIGNS  —  {date_label(date_str)}  ({len(unmapped)} camps need fixing)', NCOLS_S4)

    if unmapped:
        col_header_row(['Portal', 'Campaign Name', 'Status', 'Budget', 'Spend',
                        '1D ROAS', '7D ROAS',
                        'Category', 'Audience', 'Product', 'Missing', 'Campaign ID'])

        for i, r in enumerate(sorted(unmapped, key=lambda x: (x['portal'], x['category'], x['name']))):
            missing_fields = []
            if r['category'] == 'Unmapped': missing_fields.append('Category')
            if r['audience'] == 'Unmapped': missing_fields.append('Audience')
            if r['product']  == 'Unmapped': missing_fields.append('Product')

            row_vals = [
                r['portal'],
                r['name'],
                r['status'],
                fmt(r['budget']),
                fmt(r['spend']),
                fmt_roas(weighted_roas([r], 'rev_1d')),
                fmt_roas(weighted_roas([r], 'rev_7d')),
                r['category'] if r['category'] != 'Unmapped' else '',
                r['audience'] if r['audience'] != 'Unmapped' else '',
                r['product']  if r['product']  != 'Unmapped' else '',
                ', '.join(missing_fields),
                r['campaign_id'],
            ]
            rr = add_row(row_vals)
            data_row_fmt(rr, NCOLS_S4, is_odd=(i % 2 == 1), is_unmapped=True)
            # Highlight the missing columns in orange-red
            for col_idx, field in [(8, r['category']), (9, r['audience']), (10, r['product'])]:
                if field == 'Unmapped':
                    fmt_req(rr, col_idx, rr, col_idx, backgroundColor=rgb(231, 76, 60),
                            textFormat={'foregroundColor': rgb(255,255,255), 'bold': True})
    else:
        r = add_row(['✅  All campaigns are fully mapped!'] + [''] * (NCOLS_S4 - 1))
        fmt_req(r, 1, r, NCOLS_S4, backgroundColor=rgb(39, 174, 96),
                textFormat={'foregroundColor': rgb(255,255,255), 'bold': True})

    add_blank()
    # (freeze handled separately in write_report_tab after merges)

    return sheet_data, fmt_reqs


# ── Write tab ─────────────────────────────────────────────────────────────────
def write_report_tab(sh, date_str, sheet_data, fmt_reqs):
    tab_name = f"📊 Reports {date_label(date_str)}"
    existing = [ws.title for ws in sh.worksheets()]

    max_cols = max((len(r) for r in sheet_data), default=30)
    if tab_name in existing:
        # Delete and recreate — avoids merge conflict errors from previous runs
        old_ws = sh.worksheet(tab_name)
        sh.del_worksheet(old_ws)

    ws = sh.add_worksheet(title=tab_name, rows=max(len(sheet_data) + 20, 300), cols=max_cols + 5)
    all_ws = sh.worksheets()
    sh.reorder_worksheets([ws] + [w for w in all_ws if w.title != tab_name])

    # Patch sheetId into all format requests
    sheet_id = ws.id
    for req in fmt_reqs:
        for key in ['repeatCell', 'mergeCells', 'updateSheetProperties']:
            if key in req:
                if key == 'updateSheetProperties':
                    req[key]['properties']['sheetId'] = sheet_id
                elif 'range' in req[key]:
                    req[key]['range']['sheetId'] = sheet_id

    # Pad rows
    for row in sheet_data:
        while len(row) < max_cols:
            row.append('')

    # Write data
    ws.update(values=sheet_data, range_name='A1', value_input_option='USER_ENTERED')

    # Apply formatting
    if fmt_reqs:
        sh.batch_update({'requests': fmt_reqs})

    # Column widths + freeze col A
    try:
        sh.batch_update({'requests': [
            {'updateDimensionProperties': {
                'range': {'sheetId': sheet_id, 'dimension': 'COLUMNS', 'startIndex': 0, 'endIndex': 1},
                'properties': {'pixelSize': 200}, 'fields': 'pixelSize'
            }},
            {'updateDimensionProperties': {
                'range': {'sheetId': sheet_id, 'dimension': 'COLUMNS', 'startIndex': 1, 'endIndex': 2},
                'properties': {'pixelSize': 180}, 'fields': 'pixelSize'
            }},
            {'updateDimensionProperties': {
                'range': {'sheetId': sheet_id, 'dimension': 'COLUMNS', 'startIndex': 2, 'endIndex': max_cols},
                'properties': {'pixelSize': 110}, 'fields': 'pixelSize'
            }},
            {'updateSheetProperties': {
                'properties': {'sheetId': sheet_id, 'gridProperties': {'frozenColumnCount': 1}},
                'fields': 'gridProperties.frozenColumnCount'
            }},
        ]})
    except:
        pass

    print(f"  ✅ '{tab_name}' — {len(sheet_data)} rows, {max_cols} cols")
    return tab_name


# ── Main ──────────────────────────────────────────────────────────────────────
def aggregate_for_compare(rows):
    """
    Build comparison maps keyed by (category, type), (portal, audience), (portal, category, product).
    Returns dict of dicts: {key: {budget, spend, camps, rev_1d, sp_1d, rev_7d, sp_7d}}
    """
    cat_type   = defaultdict(lambda: {'budget':0,'spend':0,'camps':0,'rev_1d':0,'sp_1d':0,'rev_7d':0,'sp_7d':0})
    aud_cat    = defaultdict(lambda: {'budget':0,'spend':0,'camps':0,'rev_1d':0,'sp_1d':0,'rev_7d':0,'sp_7d':0})
    prod_aud   = defaultdict(lambda: {'budget':0,'spend':0,'camps':0,'rev_1d':0,'sp_1d':0,'rev_7d':0,'sp_7d':0})

    for r in rows:
        def add(d, key):
            d[key]['budget'] += r['budget']
            d[key]['spend']  += r['spend']
            d[key]['camps']  += 1
            for w in ('1d','7d'):
                rv = r.get(f'rev_{w}'); sp = r.get(f'sp_{w}', r['spend'])
                if rv is not None:
                    d[key][f'rev_{w}'] += rv
                    d[key][f'sp_{w}']  += sp

        add(cat_type, (r['category'], r['type']))
        add(aud_cat,  (r['portal'], r['audience'], r['category']))
        add(prod_aud, (r['portal'], r['category'], r['product']))

    def roas(d, key, w):
        rv = d[key][f'rev_{w}']; sp = d[key][f'sp_{w}']
        return round(rv/sp, 2) if sp else None

    # Return flat dicts with pre-computed ROAS
    result = {}
    for key, v in cat_type.items():
        result[('cat_type', key)] = {**v,
            'roas_1d': roas(cat_type, key, '1d'),
            'roas_7d': roas(cat_type, key, '7d')}
    for key, v in aud_cat.items():
        result[('aud_cat', key)] = {**v,
            'roas_1d': roas(aud_cat, key, '1d'),
            'roas_7d': roas(aud_cat, key, '7d')}
    for key, v in prod_aud.items():
        result[('prod_aud', key)] = {**v,
            'roas_1d': roas(prod_aud, key, '1d'),
            'roas_7d': roas(prod_aud, key, '7d')}
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--date', default=None)
    args = parser.parse_args()
    date_str = args.date or (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
    prev_str = (datetime.strptime(date_str, '%Y-%m-%d') - timedelta(days=1)).strftime('%Y-%m-%d')

    print(f"\n🚀 Campaign Tracker Reports — {date_str} (vs {prev_str})")

    creds = Credentials.from_service_account_file(SA_FILE, scopes=SCOPES)
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(SHEET_ID)

    print("\n📥 Loading tracker data...")
    rows = load_tracker_data(sh, date_str)
    print(f"  Total rows: {len(rows)}")

    print(f"\n📥 Loading previous day data ({prev_str})...")
    prev_rows = load_tracker_data(sh, prev_str)
    print(f"  Prev rows: {len(prev_rows)}")

    if not rows:
        print("❌ No data. Run campaign_tracker_builder.py first.")
        return

    # Build comparison maps
    prev_map = aggregate_for_compare(prev_rows) if prev_rows else {}

    print("\n📊 Building report...")
    sheet_data, fmt_reqs = build_full_report(rows, date_str, prev_map)

    print("\n✍️  Writing to sheet...")
    tab_name = write_report_tab(sh, date_str, sheet_data, fmt_reqs)

    print(f"\n✅ Done: '{tab_name}'")
    print(f"🔗 https://docs.google.com/spreadsheets/d/{SHEET_ID}")


if __name__ == '__main__':
    main()
