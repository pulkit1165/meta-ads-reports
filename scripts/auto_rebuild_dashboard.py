#!/usr/bin/env python3
"""Auto-rebuild NTN Dashboard — polls Google Sheet and rebuilds HTML on change"""

import os, re, json, requests, subprocess
from datetime import datetime
from pathlib import Path
from google.oauth2.service_account import Credentials
import gspread

# ── Path setup (Phase 2: GitHub-Actions-friendly paths) ──────────────────────
_REPO_ROOT = Path(__file__).resolve().parent.parent
STATE_DIR  = Path(os.environ.get('META_REPORTS_STATE_DIR') or (_REPO_ROOT / 'state'))
OUT_DIR    = Path(os.environ.get('META_REPORTS_OUT_DIR')   or (_REPO_ROOT / 'out'))
STATE_DIR.mkdir(parents=True, exist_ok=True)
OUT_DIR.mkdir(parents=True, exist_ok=True)

# EC2 deploy is disabled by default here — the live dashboard is still served
# by the original script on EC2. Set ENABLE_EC2_DEPLOY=1 + provide SSH_KEY/EC2
# env vars to re-enable deploy from this copy.
SSH_KEY = os.environ.get('EC2_SSH_KEY', '')
EC2 = os.environ.get('EC2_HOST', '')
SHEET_URL = 'https://docs.google.com/spreadsheets/d/1squ0JkqwiyFwIMRmqWc3q_AWHQtihn5o4dbDGyv7sAY/edit'

_SA_FILE = os.environ.get('GOOGLE_SERVICE_ACCOUNT_FILE') or str(_REPO_ROOT / 'google-service-account.json')
creds = Credentials.from_service_account_file(_SA_FILE,
    scopes=['https://www.googleapis.com/auth/spreadsheets'])
gc = gspread.authorize(creds)
sh = gc.open_by_url(SHEET_URL)
R = sh.get_worksheet(0).get_all_values()

# ── Load KPI Daily tab (Meta funnel metrics) ──────────────────────────────────
def load_kpi_daily():
    """Returns dict: { 'YYYY-MM-DD': { 'SM': {...}, 'SML': {...}, 'NBP': {...}, 'Total': {...} } }"""
    try:
        ws = sh.worksheet('KPI Daily')
        rows = ws.get_all_values()
        if not rows or len(rows) < 2:
            return {}
        headers = rows[0]
        def col(name):
            try: return headers.index(name)
            except: return None
        COL = {
            'date': col('Date'), 'portal': col('Portal'),
            'spend': col('Spend (₹)'), 'impressions': col('Impressions'),
            'reach': col('Reach'), 'frequency': col('Frequency'),
            'cpm': col('CPM (₹)'), 'ctr': col('CTR (%)'),
            'cpr': col('CPR/1L Reach (₹)'),
            'ob': col('Outbound Clicks'), 'lpv': col('LPV'),
            'atc': col('ATC'), 'atc_rate': col('ATC Rate (%)'),
            'lp_cvr': col('LP CVR (%)'), 'purchases': col('Purchases (Meta)'),
            'cpc': col('CPC (₹)'), 'cpp': col('CPP (₹)'),
            'thumbstop': col('Thumbstop (%)'), 'hold_rate': col('Hold Rate (%)'),
            'revenue': col('Revenue (Meta ₹)'), 'roas': col('ROAS'),
        }
        def safe(row, key):
            c = COL.get(key)
            if c is None or c >= len(row): return '—'
            val = str(row[c]).strip()
            if not val or val in ['0','0.0']: return '—'
            return val
        def fmt_num(val, prefix='', decimals=0):
            try:
                n = float(val)
                if n == 0: return '—'
                if decimals: return f'{prefix}{n:,.{decimals}f}'
                return f'{prefix}{n:,.0f}'
            except: return val if val else '—'

        out = {}
        for row in rows[1:]:
            if not row or not row[0]: continue
            date   = row[COL['date']] if COL['date'] is not None else ''
            portal = row[COL['portal']] if COL['portal'] is not None else ''
            if not date or not portal: continue
            if date not in out: out[date] = {}
            out[date][portal] = {
                'spend':      fmt_num(safe(row,'spend'), '₹'),
                'impressions':fmt_num(safe(row,'impressions')),
                'reach':      fmt_num(safe(row,'reach')),
                'frequency':  safe(row,'frequency'),
                'cpm':        fmt_num(safe(row,'cpm'), '₹', 0),
                'ctr':        safe(row,'ctr'),
                'cpr':        fmt_num(safe(row,'cpr'), '₹', 0),
                'ob':         fmt_num(safe(row,'ob')),
                'lpv':        fmt_num(safe(row,'lpv')),
                'atc':        fmt_num(safe(row,'atc')),
                'atc_rate':   safe(row,'atc_rate'),
                'lp_cvr':     safe(row,'lp_cvr'),
                'purchases':  fmt_num(safe(row,'purchases')),
                'cpc':        fmt_num(safe(row,'cpc'), '₹', 0),
                'cpp':        fmt_num(safe(row,'cpp'), '₹', 0),
                'thumbstop':  safe(row,'thumbstop'),
                'hold_rate':  safe(row,'hold_rate'),
                'revenue':    fmt_num(safe(row,'revenue'), '₹'),
                'roas':       safe(row,'roas'),
            }
        return out
    except Exception as e:
        print(f'[KPI Daily] load error: {e}')
        return {}

KPI_DAILY = load_kpi_daily()
print(f"[{datetime.now().strftime('%H:%M:%S')}] KPI Daily loaded: {len(KPI_DAILY)} dates")

# ── Section-header lookup ───────────────────────────────────────────────────
# The original implementation hard-coded row indices like rows[252:269] for
# 'sales_block', rows[128:135] for 'creative', etc. The source sheet has been
# edited since those indices were chosen, so each one now points at the wrong
# section. Header-based lookup is robust to row drift.
def find_section(needle, start=0):
    """Row index of the first row whose col-A starts with `needle` (case-insensitive)."""
    n = needle.lower().strip()
    for i in range(start, len(R)):
        cell = (R[i][0] or '').strip().lower()
        if cell.startswith(n):
            return i
    return -1

def section_data_rows(header_row, max_rows=60):
    """Yield row indices that contain data (non-blank col A) starting after the
    header_row + 1 (skip likely sub-header) and stopping at the next blank row."""
    if header_row < 0:
        return []
    out = []
    # First non-blank row after header is usually a column-headers row (e.g. "Type | ...").
    # We don't know in advance so we yield ALL non-blank rows; callers filter on col A.
    i = header_row + 1
    while i < len(R) and i - header_row <= max_rows:
        row = R[i] if i < len(R) else []
        a = (row[0] or '').strip()
        rest = any((row[k] or '').strip() for k in range(1, min(6, len(row))))
        if not a and not rest:
            break
        out.append(i)
        i += 1
    return out

def v(r,c):
    try: return str(R[r][c]).replace('₹','').replace(',','').strip() or '-'
    except: return '-'

def vraw(r, c):
    """Like v() but preserves ₹ + comma formatting (used for cells we render literally)."""
    try: return (R[r][c] or '').strip() or '-'
    except: return '-'

# Detect all date columns dynamically
n = sum(1 for x in R[2][1:] if x.strip()) + 1
DATES_RAW = [R[2][i] for i in range(1, n)]
DL = [d.replace('-2026','') for d in DATES_RAW]
print(f"[{datetime.now().strftime('%H:%M:%S')}] Dates detected: {DL}")

def arr(r, s=1): return [v(r,i) for i in range(s, n)]

# ── Header-based section locations (resilient to row drift) ─────────────────
ORDERS_HDR    = find_section('Orders Report')        # row of section header
SM_OPEN       = find_section('Audience - SM')
SML_OPEN      = find_section('Audience - SML')
NBP_OPEN      = find_section('Audience - NBP')
CREATIVE_HDR  = find_section('Creative performance')  # creative perf section
SM_PROD       = find_section('Products - SM')
SML_PROD      = find_section('Products - SML')
NBP_PROD      = find_section('Products - NBP')
SALES_BLK     = find_section('Sales Block Opening')
NR_MASTER_HDR = find_section('New Returning- Master')
NR_PORTAL_HDR = find_section('New Returning- Portal')
C16_MASTER    = find_section('C1-C6 Count Pending %age ( Master')
C16_PORTAL    = find_section('C1-C6 Count Pending %age ( Portal')
print(f"[sections] orders={ORDERS_HDR} creative={CREATIVE_HDR} sales_block={SALES_BLK} "
      f"c16_master={C16_MASTER} c16_portal={C16_PORTAL} nr_master={NR_MASTER_HDR}")

def kpi_row(header_row, label_substr):
    """Find a row by label within the section that starts at header_row.
    Returns the row index, or None."""
    if header_row < 0:
        return None
    for i in range(header_row + 1, min(len(R), header_row + 30)):
        cell = (R[i][0] or '').strip().lower()
        if cell.startswith(label_substr.lower()):
            return i
        if not cell and not any((R[i][k] or '').strip() for k in range(1, min(6, len(R[i])))):
            break
    return None

def kpi_arr(header_row, label_substr, start_col=1):
    """Pull the date-aligned array for the row whose label starts with label_substr."""
    r = kpi_row(header_row, label_substr)
    if r is None:
        return ['-'] * len(DL)
    return [v(r, c) for c in range(start_col, start_col + len(DL))]

# Section: Sales Block Opening Report (the real one — not the C1-C6 misalignment)
def sales_block_rows():
    """Read every data row in the Sales Block section until 'Grand Total' or section break."""
    out = []
    if SALES_BLK < 0:
        return out
    # Sub-header row immediately after section title is "TOF | New Remarks | NBP | SM | SML | Total"
    for i in section_data_rows(SALES_BLK + 1):  # +1 to skip the column-header row
        row = R[i]
        a = (row[0] or '').strip()
        if a in ('TOF', 'New Remarks', ''): continue
        out.append([v(i, j) for j in range(6)])
        if a.lower().startswith('grand total'):
            break
    return out

# Section: Creative performance (Type | Amt Spent | Roas)
def creative_rows_data():
    if CREATIVE_HDR < 0:
        return []
    out = []
    for i in section_data_rows(CREATIVE_HDR + 1):
        a = (R[i][0] or '').strip()
        if a in ('', 'Type', 'Creative Performance'): continue
        # Data layout: Type | Amt Spent | Roas
        amt   = v(i, 1)
        roas  = v(i, 2)
        out.append((a, roas, amt))
        if a.lower().startswith('grand total'):
            break
    return out

# Section: Products by portal (Product | Avg Roas | Budget)
def product_rows_data(header_row):
    if header_row < 0:
        return []
    out = []
    for i in section_data_rows(header_row + 1):
        a = (R[i][0] or '').strip()
        if a in ('', 'Product'): continue
        roas = v(i, 1)
        bud  = v(i, 2)
        out.append((a, roas, bud))
        if a.lower().startswith('grand total'):
            break
    return out

# Section: CPM rows from the per-portal metrics table (rows around 54-72)
def cpm_for_portal(portal):
    """Find the CPM row for `portal` and return its date-aligned values."""
    # The per-portal metrics block has rows like (Portal | Metrics | val_per_date...).
    # We look for col A == portal AND col B == 'CPM'.
    for i in range(len(R)):
        a = (R[i][0] or '').strip().upper()
        b = (R[i][1] or '').strip().upper()
        if a == portal and b == 'CPM':
            return [v(i, c) for c in range(2, 2 + len(DL))]
    return ['-'] * len(DL)

# Section: New / Returning — Master file (rows after NR_MASTER_HDR)
def nr_master_arr(label):
    if NR_MASTER_HDR < 0:
        return ['-'] * len(DL)
    for i in range(NR_MASTER_HDR + 1, min(len(R), NR_MASTER_HDR + 8)):
        a = (R[i][0] or '').strip().lower()
        if a == label.lower():
            return [v(i, c) for c in range(1, 1 + len(DL))]
    return ['-'] * len(DL)

# Section: New / Returning — Portal-wise
def nr_portal_arr(portal, metric):
    if NR_PORTAL_HDR < 0:
        return ['-'] * len(DL)
    for i in range(NR_PORTAL_HDR + 1, min(len(R), NR_PORTAL_HDR + 12)):
        a = (R[i][0] or '').strip().upper()
        b = (R[i][1] or '').strip().lower()
        if a == portal and b == metric.lower():
            return [v(i, c) for c in range(2, 2 + len(DL))]
    return ['-'] * len(DL)

# Section: C1-C6 cohort percentages
def c16_cohort(header_row, portal):
    """Returns [{'cohort': 'C1', 'values': [v_per_date...]}, ...] for one portal."""
    if header_row < 0:
        return []
    out = []
    for i in range(header_row + 1, min(len(R), header_row + 30)):
        a = (R[i][0] or '').strip().upper()
        b = (R[i][1] or '').strip().upper()
        if a == portal and b in ('C1', 'C2', 'C3', 'C4', 'C5', 'C6'):
            out.append({'cohort': b, 'values': [v(i, c) for c in range(2, 2 + len(DL))]})
        elif a == f'{portal} TOTAL':
            out.append({'cohort': 'Total', 'values': [v(i, c) for c in range(2, 2 + len(DL))]})
            break
    return out

D = {
    'dates': DATES_RAW,
    'orders':  kpi_arr(ORDERS_HDR, 'Orders'),
    'revenue': kpi_arr(ORDERS_HDR, 'Revenue'),
    'adspend': kpi_arr(ORDERS_HDR, 'Ad Spend'),
    'roas':    kpi_arr(ORDERS_HDR, 'Roas'),
    # New / Returning percentages
    'new_m':   nr_master_arr('New'),
    'ret_m':   nr_master_arr('Returning'),
    'sm_new':  nr_portal_arr('SM',  'New'),
    'sm_ret':  nr_portal_arr('SM',  'Returning'),
    'sml_new': nr_portal_arr('SML', 'New'),
    'sml_ret': nr_portal_arr('SML', 'Returning'),
    'nbp_new': nr_portal_arr('NBP', 'New'),
    'nbp_ret': nr_portal_arr('NBP', 'Returning'),
    # CPMs from the per-portal metrics block
    'sm_cpm':  cpm_for_portal('SM'),
    'sml_cpm': cpm_for_portal('SML'),
    'nbp_cpm': cpm_for_portal('NBP'),
    # Tables
    'creative':   creative_rows_data(),
    'sm_prods':   product_rows_data(SM_PROD),
    'sml_prods':  product_rows_data(SML_PROD),
    'nbp_prods':  product_rows_data(NBP_PROD),
    'sales_block': sales_block_rows(),
    # C1-C6 actuals (master + portal-wise variants)
    'c16_master_sm':  c16_cohort(C16_MASTER, 'SM'),
    'c16_master_sml': c16_cohort(C16_MASTER, 'SML'),
    'c16_master_nbp': c16_cohort(C16_MASTER, 'NBP'),
    'c16_portal_sm':  c16_cohort(C16_PORTAL, 'SM'),
    'c16_portal_sml': c16_cohort(C16_PORTAL, 'SML'),
    'c16_portal_nbp': c16_cohort(C16_PORTAL, 'NBP'),
    'updated': datetime.now().strftime('%d %b %Y, %I:%M %p IST')
}

# ── C1-C6 targets (from GHA sheet, editable by user) ───────────────────────
# Targets live in the GHA-owned sheet, tab 'C1-C6 Targets'. If the tab doesn't
# exist yet we auto-create it with sensible defaults so the user can edit and
# re-run. Layout: rows are 'Portal | Cohort | Target %', e.g. 'SM | C1 | 30%'.
def load_c16_targets():
    """Returns {('SM','C1'): 30, ...} as int %s."""
    gha_sid = os.environ.get('REPORTS_SHEET_ID') or '1hJ3IS2VDtTAEyyJIV__jvts9CMQdYhyxKAfWKtrkUH4'
    try:
        gha_sh = gc.open_by_key(gha_sid)
    except Exception as e:
        print(f"[c16-targets] cannot open GHA sheet: {e}")
        return {}
    titles = [w.title for w in gha_sh.worksheets()]
    if 'C1-C6 Targets' not in titles:
        # Bootstrap a default targets tab (user edits later)
        try:
            ws = gha_sh.add_worksheet(title='C1-C6 Targets', rows=30, cols=4)
            seed = [['Portal', 'Cohort', 'Target % (Pending — lower is better)', 'Notes']]
            # Defaults: tighter targets for early cohorts (more first-purchase activation expected)
            defaults = {
                'C1': 30, 'C2': 50, 'C3': 70, 'C4': 75, 'C5': 80, 'C6': 90,
            }
            for portal in ('SM', 'SML', 'NBP'):
                for c in ('C1','C2','C3','C4','C5','C6'):
                    seed.append([portal, c, defaults[c], 'edit me — % pending allowed'])
            ws.update(range_name='A1', values=seed, value_input_option='USER_ENTERED')
            print("[c16-targets] bootstrapped 'C1-C6 Targets' tab in GHA sheet")
        except Exception as e:
            print(f"[c16-targets] bootstrap error: {e}")
            return {}
    try:
        ws = gha_sh.worksheet('C1-C6 Targets')
        rows = ws.get_all_values()
    except Exception as e:
        print(f"[c16-targets] read error: {e}")
        return {}
    out = {}
    for r in rows[1:]:
        if len(r) < 3: continue
        portal, cohort, tgt = (r[0] or '').strip().upper(), (r[1] or '').strip().upper(), (r[2] or '').strip()
        if not portal or cohort not in ('C1','C2','C3','C4','C5','C6'): continue
        try:
            out[(portal, cohort)] = int(str(tgt).rstrip('%').strip() or 0)
        except ValueError:
            continue
    return out

C16_TARGETS = load_c16_targets()
print(f"[c16-targets] loaded {len(C16_TARGETS)} targets")

# ── Today's Live snapshot from GHA sheet (for live-vs-static comparison) ───
def load_today_live():
    """Pull a few key live metrics from the GHA sheet's '🔴 Today's Live ...' tab.
    Returns dict with current spend, current ROAS, success-rate buckets, top creative.
    Best-effort: returns empty dict if tab not found."""
    gha_sid = os.environ.get('REPORTS_SHEET_ID') or '1hJ3IS2VDtTAEyyJIV__jvts9CMQdYhyxKAfWKtrkUH4'
    try:
        gha_sh = gc.open_by_key(gha_sid)
    except Exception:
        return {}
    today_label = datetime.now().strftime('%d %b %y').upper()
    candidates = [w.title for w in gha_sh.worksheets() if today_label in w.title and 'Live' in w.title]
    if not candidates:
        return {}
    try:
        ws = gha_sh.worksheet(candidates[0])
        rows = ws.get_all_values()
    except Exception:
        return {}
    out = {'tab_name': candidates[0], 'updated': '—'}
    for r in rows:
        cell = (r[0] or '')
        # Update timestamp from the section header
        if cell.startswith('📊') and 'Updated' in cell:
            out['updated'] = cell.split('Updated', 1)[-1].strip().rstrip('|').strip()
        # Total spend today + total camps + buckets
        if cell == 'TOTAL TODAY' and len(r) > 4:
            out['total_camps'] = r[2]
            out['total_spend'] = r[3]
        if '≥ 1.5x' in cell and len(r) > 4:
            out['pct_above_1_5'] = r[4]
        if 'Below 1.0x' in cell and len(r) > 4:
            out['pct_below_1'] = r[4]
    return out

LIVE = load_today_live()
print(f"[today-live] {'loaded ' + LIVE.get('tab_name','—') if LIVE else 'no Today Live tab found yet'}")

# Build KPI
kpi = {}
for i,dl in enumerate(DL):
    rv = D['roas'][i] if i < len(D['roas']) else '-'
    o  = D['orders'][i] if i < len(D['orders']) else '-'
    r  = D['revenue'][i] if i < len(D['revenue']) else '-'
    s  = D['adspend'][i] if i < len(D['adspend']) else '-'
    kpi[dl] = {
        'o': o if o!='-' else '—', 'r': '₹'+r if r!='-' else '—',
        's': '₹'+s if s!='-' else '—', 'roas': rv+'x' if rv!='-' else '—',
        'rv': float(rv) if rv not in ['-',''] else 0
    }
kpi['all'] = {'o':'—','r':'—','s':'—','roas':'—','rv':0}

# Map KPI Daily by display-label date (e.g. "19-Apr" → KPI_DAILY["2026-04-19"])
def kpi_daily_for_dl(dl):
    """Match display label like '19-Apr' or '09-Apr' to KPI_DAILY date keys."""
    for raw_date in KPI_DAILY:
        try:
            dt = datetime.strptime(raw_date, '%Y-%m-%d')
            label = dt.strftime('%-d-%b')   # '19-Apr'
            label0 = dt.strftime('%d-%b')   # '09-Apr' (zero-padded)
            if dl == label or dl == label0:
                return KPI_DAILY[raw_date]
        except: pass
    return None

def date_btns():
    b = ''
    last_idx = len(DL) - 1
    for i,dl in enumerate(DL):
        act = ' active' if i==last_idx else ''
        b += f"<button class='date-btn{act}' onclick='setDate(\"{dl}\",this)'>{dl}</button>"
    b += "<button class='date-btn' onclick='setDate(\"all\",this)' style='border-style:dashed'>All</button>"
    return b

def th_cols(): return ''.join(f'<th>{dl}</th>' for dl in DL)
def tbl_row(lbl, arr_key):
    a = D.get(arr_key, [])
    tds = ''.join(f'<td>{a[i] if i<len(a) else "-"}</td>' for i in range(len(DL)))
    return f'<tr><td>{lbl}</td>{tds}</tr>'

def _pct_to_float(v):
    try: return float(str(v).rstrip('%').strip())
    except (ValueError, TypeError): return None

def nr_rows():
    """New/Returning table — adds 7-day average + today vs avg delta + trend arrow.
    Latest date = last column; 7-day avg = mean of the last 7 dates that have data."""
    series = [
        ('New % (Master)',     'new_m'),
        ('Returning % (Master)','ret_m'),
        ('SM — New',           'sm_new'),
        ('SM — Returning',     'sm_ret'),
        ('SML — New',          'sml_new'),
        ('SML — Returning',    'sml_ret'),
        ('NBP — New',          'nbp_new'),
        ('NBP — Returning',    'nbp_ret'),
    ]
    rows = ''
    for lbl, key in series:
        a = D.get(key, [])
        tds = ''.join(f'<td>{a[i] if i<len(a) else "-"}</td>' for i in range(len(DL)))

        # Analytics columns
        nums = [_pct_to_float(x) for x in a]
        valid = [n for n in nums if n is not None]
        latest = next((n for n in reversed(nums) if n is not None), None)
        prior7 = [n for n in nums[-8:-1] if n is not None]
        avg7   = (sum(prior7) / len(prior7)) if prior7 else None

        avg_cell = f'{avg7:.1f}%' if avg7 is not None else '—'
        if latest is not None and avg7 is not None:
            delta = latest - avg7
            arrow = '↑' if delta > 0.5 else ('↓' if delta < -0.5 else '→')
            color = '#0d6e3a' if delta > 0.5 else ('#a3260a' if delta < -0.5 else '#666')
            delta_cell = f'<span style="color:{color};font-weight:700">{arrow} {delta:+.1f}pp</span>'
        else:
            delta_cell = '—'
        rows += f'<tr><td>{lbl}</td>{tds}<td style="background:#f5f7ff">{avg_cell}</td><td style="background:#f5f7ff">{delta_cell}</td></tr>'
    return rows

def nr_th_cols():
    """Header cells for the N/R table (dates + 7-day avg + delta)."""
    return th_cols() + '<th>7-day Avg</th><th>Today vs Avg</th>'

def c16_rows(cohorts, portal):
    """Render C1-C6 actuals for one portal. Adds Target and vs-Target columns.
    Color: green if actual ≤ target, amber within 10pp, red if > target+10pp.
    (Lower % pending is better.)"""
    rows = ''
    for c in cohorts:
        co = c['cohort']
        vals = c['values']
        tds = ''.join(f'<td>{v if v else "-"}</td>' for v in vals)
        latest_pct = _pct_to_float(next((v for v in reversed(vals) if v), None))
        target = C16_TARGETS.get((portal.upper(), co))
        if co == 'Total' or target is None or latest_pct is None:
            tgt_cell = '—'
            cmp_cell = '—'
        else:
            tgt_cell = f'{target}%'
            diff = latest_pct - target
            if diff <= 0:
                cmp_cell = f'<span style="color:#0d6e3a;font-weight:700">✓ {diff:+.0f}pp</span>'
            elif diff <= 10:
                cmp_cell = f'<span style="color:#a35a00;font-weight:700">⚠ +{diff:.0f}pp</span>'
            else:
                cmp_cell = f'<span style="color:#a3260a;font-weight:700">✗ +{diff:.0f}pp</span>'
        is_total = co == 'Total'
        bg = 'background:#eef2ff;font-weight:700' if is_total else ''
        rows += f'<tr style="{bg}"><td>{co}</td>{tds}<td>{tgt_cell}</td><td>{cmp_cell}</td></tr>'
    return rows

def creative_rows():
    rows = ''
    for c in D['creative']:
        t,r,s = c
        if t in ['','Type','Creative Performance']: continue
        is_total = 'Total' in t or 'Grand' in t
        try: rv=float(r); cls='rg' if rv>=2 else 'ro' if rv>=1.5 else 'rr'
        except: cls='ro'
        s = s.replace(' ','') if s not in ['-',''] else s
        if is_total: rows+=f'<tr style="font-weight:700;background:#eef2ff"><td>{t}</td><td><b>{r}x</b></td><td>₹{s}</td></tr>'
        else: rows+=f'<tr><td>{t}</td><td class="{cls}">{r}x</td><td>₹{s}</td></tr>'
    return rows

def prod_rows(key):
    rows = ''
    for p in D[key]:
        nm,r,bg = p
        is_total = 'Grand' in nm
        bg = bg.replace(' ','') if bg not in ['-',''] else bg
        try: rv=float(r); cls='bg' if rv>=2 else 'bo' if rv>=1.5 else 'br'; badge=f'<span class="badge {cls}">{r}x</span>'
        except: badge=f'<b>{r}</b>'
        if is_total: rows+=f'<tr style="font-weight:700;background:#eef2ff"><td>{nm}</td><td><b>{r}x</b></td><td>₹{bg}</td></tr>'
        else: rows+=f'<tr><td style="font-size:11px">{nm}</td><td>{badge}</td><td>₹{bg}</td></tr>'
    return rows

def sb_rows():
    rows = ''
    for sb in D['sales_block']:
        tof=(sb[0] if len(sb)>0 else ''); rem=(sb[1] if len(sb)>1 else '')
        nbp=(sb[2] if len(sb)>2 else '-'); sm=(sb[3] if len(sb)>3 else '-')
        sml=(sb[4] if len(sb)>4 else '-'); tot=(sb[5] if len(sb)>5 else '-')
        def fmt(x): return f'₹{x}' if x not in ['-',''] else '-'
        if tof=='Grand Total':
            rows+=f'<tr style="font-weight:700;background:#eef2ff"><td colspan="2"><b>Grand Total</b></td><td><b>{fmt(nbp)}</b></td><td><b>{fmt(sm)}</b></td><td><b>{fmt(sml)}</b></td><td><b>{fmt(tot)}</b></td></tr>'
        else:
            rows+=f'<tr><td>{tof}</td><td style="font-size:11px">{rem}</td><td>{fmt(nbp)}</td><td>{fmt(sm)}</td><td>{fmt(sml)}</td><td>{fmt(tot)}</td></tr>'
    return rows

def ch_arr(key): return json.dumps([float(x) if x not in ['-',''] else None for x in D.get(key,[])])
def ord_arr(): return json.dumps([float(str(x).replace(',','')) if x not in ['-',''] else None for x in D['orders']])

html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>NTN Dashboard</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:'Segoe UI',system-ui,sans-serif;background:#f0f4fb;color:#1a1a2e}}
.header{{background:linear-gradient(135deg,#0d2145,#1a3d7c);padding:20px 28px;display:flex;align-items:center;justify-content:space-between}}
.header h1{{color:#fff;font-size:20px;font-weight:700}}.header p{{color:rgba(255,255,255,.6);font-size:11px;margin-top:3px}}
.last-upd{{color:rgba(255,255,255,.45);font-size:11px}}
.slicer{{background:#fff;border-bottom:1px solid #dde3f0;padding:12px 28px;display:flex;align-items:center;gap:10px;position:sticky;top:0;z-index:100;box-shadow:0 2px 8px rgba(0,0,0,.06);flex-wrap:wrap}}
.sl-label{{font-size:11px;font-weight:700;color:#6b7280;text-transform:uppercase;letter-spacing:.5px}}
.date-btn{{padding:6px 14px;border-radius:20px;border:2px solid #dde3f0;background:#fff;color:#6b7280;font-size:12px;font-weight:600;cursor:pointer;transition:all .2s}}
.date-btn:hover{{border-color:#1a3d7c;color:#1a3d7c}}
.date-btn.active{{background:#1a3d7c;color:#fff;border-color:#1a3d7c}}
.main{{max-width:1400px;margin:0 auto;padding:24px 28px}}
.kpi-grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-bottom:24px}}
.kpi{{background:#fff;border-radius:12px;padding:20px;border:1px solid #dde3f0;box-shadow:0 2px 10px rgba(0,0,0,.04);position:relative;overflow:hidden;transition:transform .15s}}
.kpi:hover{{transform:translateY(-2px)}}
.kpi::before{{content:'';position:absolute;top:0;left:0;width:4px;height:100%}}
.kpi.orders::before{{background:#1a3d7c}}.kpi.rev::before{{background:#00a878}}.kpi.spend::before{{background:#f5c518}}.kpi.rk::before{{background:#9b59b6}}
.kpi-icon{{font-size:24px;margin-bottom:8px}}.kpi-lbl{{font-size:10px;color:#6b7280;text-transform:uppercase;letter-spacing:.8px;font-weight:700}}
.kpi-val{{font-size:26px;font-weight:900;color:#0d2145;margin:5px 0 3px}}.kpi-sub{{font-size:11px}}
.badge{{padding:2px 10px;border-radius:20px;font-size:10px;font-weight:700;display:inline-block}}
.bg{{background:#c6f6d5;color:#276749}}.bo{{background:#feebc8;color:#744210}}.br{{background:#fed7d7;color:#742a2a}}
.charts-row{{display:grid;grid-template-columns:1fr 1fr 1fr;gap:18px;margin-bottom:24px}}
.chart-card{{background:#fff;border-radius:12px;padding:20px;border:1px solid #dde3f0;box-shadow:0 2px 10px rgba(0,0,0,.04)}}
.chart-title{{font-size:12px;font-weight:700;color:#0d2145;margin-bottom:12px}}
canvas{{width:100%!important}}
.tables-row{{display:grid;grid-template-columns:1fr 1fr;gap:18px;margin-bottom:24px}}
.tbl-card{{background:#fff;border-radius:12px;padding:20px;border:1px solid #dde3f0;box-shadow:0 2px 10px rgba(0,0,0,.04)}}
.tbl-card.full{{grid-column:1/-1}}
.sec-ttl{{font-size:12px;font-weight:700;color:#0d2145;margin-bottom:12px;padding-bottom:8px;border-bottom:2px solid #eef2ff}}
table{{width:100%;border-collapse:collapse;font-size:12px}}
th{{background:#0d2145;color:#fff;padding:8px 10px;text-align:left;font-size:10px;letter-spacing:.3px}}
th:not(:first-child){{text-align:center}}
td{{padding:8px 10px;border-bottom:1px solid #eef2ff}}
td:not(:first-child){{text-align:center;font-weight:600}}
tr:nth-child(even) td{{background:#f8faff}}tr:hover td{{background:#eef7ff}}
.rg{{color:#276749}}.ro{{color:#dd6b20}}.rr{{color:#e53e3e}}
.prods{{display:grid;grid-template-columns:1fr 1fr 1fr;gap:18px;margin-bottom:24px}}
footer{{text-align:center;padding:16px;color:#9ca3af;font-size:10px;border-top:1px solid #dde3f0}}
@media(max-width:900px){{.kpi-grid,.charts-row,.tables-row,.prods,.meta-kpi-grid{{grid-template-columns:1fr}}}}
.meta-section{{margin-bottom:28px}}
.meta-sec-hdr{{display:flex;align-items:center;justify-content:space-between;margin-bottom:16px;padding-bottom:10px;border-bottom:3px solid #eef2ff}}
.meta-sec-hdr h2{{font-size:14px;font-weight:800;color:#0d2145}}
.meta-sec-hdr span{{font-size:11px;color:#9ca3af}}
/* Category group headers */
.kpi-group{{margin-bottom:20px}}
.kpi-group-label{{display:flex;align-items:center;gap:8px;margin-bottom:10px}}
.kpi-group-label span{{font-size:10px;font-weight:800;text-transform:uppercase;letter-spacing:1px;padding:3px 12px;border-radius:20px}}
.lbl-media{{background:#dbeafe;color:#1d4ed8}}
.lbl-video{{background:#ede9fe;color:#6d28d9}}
.lbl-funnel{{background:#fef3c7;color:#92400e}}
.lbl-conv{{background:#d1fae5;color:#065f46}}
/* Metric cards grid */
.mk-grid{{display:grid;gap:10px;margin-bottom:4px}}
.mk-grid-5{{grid-template-columns:repeat(5,1fr)}}
.mk-grid-4{{grid-template-columns:repeat(4,1fr)}}
.mk-grid-6{{grid-template-columns:repeat(6,1fr)}}
/* Individual metric card */
.mk{{border-radius:12px;padding:14px 16px;position:relative;overflow:hidden;transition:transform .15s,box-shadow .15s}}
.mk:hover{{transform:translateY(-2px);box-shadow:0 8px 24px rgba(0,0,0,.1)}}
.mk-media{{background:linear-gradient(135deg,#eff6ff,#dbeafe);border:1px solid #bfdbfe}}
.mk-video{{background:linear-gradient(135deg,#f5f3ff,#ede9fe);border:1px solid #ddd6fe}}
.mk-funnel{{background:linear-gradient(135deg,#fffbeb,#fef3c7);border:1px solid #fde68a}}
.mk-conv{{background:linear-gradient(135deg,#ecfdf5,#d1fae5);border:1px solid #a7f3d0}}
.mk-icon{{font-size:20px;margin-bottom:6px}}
.mk-lbl{{font-size:9px;font-weight:800;text-transform:uppercase;letter-spacing:.8px;opacity:.7;margin-bottom:4px}}
.mk-media .mk-lbl{{color:#1d4ed8}}.mk-video .mk-lbl{{color:#6d28d9}}
.mk-funnel .mk-lbl{{color:#92400e}}.mk-conv .mk-lbl{{color:#065f46}}
.mk-val{{font-size:22px;font-weight:900;color:#0d2145;margin-bottom:2px;line-height:1}}
.mk-portal-row{{display:flex;gap:6px;margin-top:6px;flex-wrap:wrap}}
.mk-portal-pill{{font-size:9px;font-weight:700;padding:2px 7px;border-radius:10px;display:flex;align-items:center;gap:3px}}
.pill-sm{{background:#dbeafe;color:#1d4ed8}}
.pill-sml{{background:#d1fae5;color:#065f46}}
.pill-nbp{{background:#fef3c7;color:#92400e}}
.pill-total{{background:#f3e8ff;color:#6d28d9}}
/* Portal comparison table */
.meta-table-wrap{{background:#fff;border-radius:14px;padding:20px;border:1px solid #dde3f0;box-shadow:0 2px 10px rgba(0,0,0,.04);margin-bottom:20px;overflow-x:auto}}
.meta-tbl{{width:100%;border-collapse:collapse;font-size:12px;min-width:680px}}
.meta-tbl th{{padding:10px 14px;font-size:10px;font-weight:800;text-transform:uppercase;letter-spacing:.5px;text-align:center}}
.meta-tbl th:first-child{{text-align:left}}
.meta-tbl th.h-sm{{background:#dbeafe;color:#1d4ed8}}
.meta-tbl th.h-sml{{background:#d1fae5;color:#065f46}}
.meta-tbl th.h-nbp{{background:#fef3c7;color:#92400e}}
.meta-tbl th.h-total{{background:#f3e8ff;color:#6d28d9}}
.meta-tbl th.h-metric{{background:#f8faff;color:#374151}}
.meta-tbl td{{padding:9px 14px;border-bottom:1px solid #f0f4ff;text-align:center;font-weight:600;font-size:12px}}
.meta-tbl td:first-child{{text-align:left;font-weight:700;font-size:11px;color:#374151}}
.meta-tbl tr:hover td{{background:#f8faff}}
.meta-tbl .group-hdr td{{background:#f0f4ff;font-weight:800;font-size:10px;text-transform:uppercase;letter-spacing:.5px;color:#6b7280;padding:6px 14px}}
.val-good{{color:#059669;font-weight:800}}.val-warn{{color:#d97706;font-weight:800}}.val-bad{{color:#dc2626;font-weight:800}}.val-neu{{color:#374151}}
</style>
</head>
<body>
<div class="header">
  <div><h1>📊 NTN Performance Dashboard</h1><p>Day-wise Reporting · Apr 2026 · SM + SML + NBP</p></div>
  <span class="last-upd">🕐 {D['updated']}</span>
</div>
<div class="slicer">
  <span class="sl-label">📅 Date</span>
  {date_btns()}
</div>
<div class="main">
  <div class="kpi-grid">
    <div class="kpi orders"><div class="kpi-icon">📦</div><div class="kpi-lbl">Orders</div><div class="kpi-val" id="v-o">—</div><div class="kpi-sub">All portals</div></div>
    <div class="kpi rev"><div class="kpi-icon">💰</div><div class="kpi-lbl">Revenue</div><div class="kpi-val" id="v-r">—</div><div class="kpi-sub">SM+SML+NBP</div></div>
    <div class="kpi spend"><div class="kpi-icon">📢</div><div class="kpi-lbl">Ad Spend</div><div class="kpi-val" id="v-s">—</div><div class="kpi-sub">Meta Ads</div></div>
    <div class="kpi rk"><div class="kpi-icon">🎯</div><div class="kpi-lbl">ROAS</div><div class="kpi-val" id="v-roas">—</div><div class="kpi-sub" id="v-badge"></div></div>
  </div>
  <div class="tables-row">
    <div class="tbl-card full">
      <div class="sec-ttl">📊 Orders & Revenue Summary</div>
      <table><thead><tr><th>Metric</th>{th_cols()}</tr></thead><tbody>
        {tbl_row('Orders','orders')}{tbl_row('Revenue (₹)','revenue')}{tbl_row('Ad Spend (₹)','adspend')}{tbl_row('ROAS','roas')}
      </tbody></table>
    </div>
  </div>
  <!-- ═══ Meta Ads KPI Section ═══ -->
  <div class="meta-section">
    <div class="meta-sec-hdr">
      <h2>📡 Meta Ads KPIs — All Portals at a Glance</h2>
      <span id="meta-date-label">Select a date above</span>
    </div>

    <!-- ── Gradient Metric Cards ── -->
    <!-- 📊 Media -->
    <div class="kpi-group">
      <div class="kpi-group-label"><span class="lbl-media">📊 Media Metrics</span></div>
      <div class="mk-grid mk-grid-5">
        <div class="mk mk-media"><div class="mk-icon">👁</div><div class="mk-lbl">Impressions</div>
          <div class="mk-val" id="m-imp-T">—</div>
          <div class="mk-portal-row">
            <span class="mk-portal-pill pill-sm">SM <b id="m-imp-SM">—</b></span>
            <span class="mk-portal-pill pill-sml">SML <b id="m-imp-SML">—</b></span>
            <span class="mk-portal-pill pill-nbp">NBP <b id="m-imp-NBP">—</b></span>
          </div></div>
        <div class="mk mk-media"><div class="mk-icon">🎯</div><div class="mk-lbl">Reach (Unique)</div>
          <div class="mk-val" id="m-reach-T">—</div>
          <div class="mk-portal-row">
            <span class="mk-portal-pill pill-sm">SM <b id="m-reach-SM">—</b></span>
            <span class="mk-portal-pill pill-sml">SML <b id="m-reach-SML">—</b></span>
            <span class="mk-portal-pill pill-nbp">NBP <b id="m-reach-NBP">—</b></span>
          </div></div>
        <div class="mk mk-media"><div class="mk-icon">🔁</div><div class="mk-lbl">Frequency</div>
          <div class="mk-val" id="m-freq-T">—</div>
          <div class="mk-portal-row">
            <span class="mk-portal-pill pill-sm">SM <b id="m-freq-SM">—</b></span>
            <span class="mk-portal-pill pill-sml">SML <b id="m-freq-SML">—</b></span>
            <span class="mk-portal-pill pill-nbp">NBP <b id="m-freq-NBP">—</b></span>
          </div></div>
        <div class="mk mk-media"><div class="mk-icon">💸</div><div class="mk-lbl">CPM (₹)</div>
          <div class="mk-val" id="m-cpm-T">—</div>
          <div class="mk-portal-row">
            <span class="mk-portal-pill pill-sm">SM <b id="m-cpm-SM">—</b></span>
            <span class="mk-portal-pill pill-sml">SML <b id="m-cpm-SML">—</b></span>
            <span class="mk-portal-pill pill-nbp">NBP <b id="m-cpm-NBP">—</b></span>
          </div></div>
        <div class="mk mk-media"><div class="mk-icon">📌</div><div class="mk-lbl">CPR / 1L Reach (₹)</div>
          <div class="mk-val" id="m-cpr-T">—</div>
          <div class="mk-portal-row">
            <span class="mk-portal-pill pill-sm">SM <b id="m-cpr-SM">—</b></span>
            <span class="mk-portal-pill pill-sml">SML <b id="m-cpr-SML">—</b></span>
            <span class="mk-portal-pill pill-nbp">NBP <b id="m-cpr-NBP">—</b></span>
          </div></div>
      </div>
    </div>

    <!-- 🎥 Video -->
    <div class="kpi-group">
      <div class="kpi-group-label"><span class="lbl-video">🎥 Video & Engagement</span></div>
      <div class="mk-grid mk-grid-4">
        <div class="mk mk-video"><div class="mk-icon">▶️</div><div class="mk-lbl">Thumbstop %</div>
          <div class="mk-val" id="m-thumbstop-T">—</div>
          <div class="mk-portal-row">
            <span class="mk-portal-pill pill-sm">SM <b id="m-thumbstop-SM">—</b></span>
            <span class="mk-portal-pill pill-sml">SML <b id="m-thumbstop-SML">—</b></span>
            <span class="mk-portal-pill pill-nbp">NBP <b id="m-thumbstop-NBP">—</b></span>
          </div></div>
        <div class="mk mk-video"><div class="mk-icon">⏱</div><div class="mk-lbl">Hold Rate %</div>
          <div class="mk-val" id="m-hold-T">—</div>
          <div class="mk-portal-row">
            <span class="mk-portal-pill pill-sm">SM <b id="m-hold-SM">—</b></span>
            <span class="mk-portal-pill pill-sml">SML <b id="m-hold-SML">—</b></span>
            <span class="mk-portal-pill pill-nbp">NBP <b id="m-hold-NBP">—</b></span>
          </div></div>
        <div class="mk mk-video"><div class="mk-icon">📊</div><div class="mk-lbl">CTR %</div>
          <div class="mk-val" id="m-ctr-T">—</div>
          <div class="mk-portal-row">
            <span class="mk-portal-pill pill-sm">SM <b id="m-ctr-SM">—</b></span>
            <span class="mk-portal-pill pill-sml">SML <b id="m-ctr-SML">—</b></span>
            <span class="mk-portal-pill pill-nbp">NBP <b id="m-ctr-NBP">—</b></span>
          </div></div>
        <div class="mk mk-video"><div class="mk-icon">🖱</div><div class="mk-lbl">CPC (₹)</div>
          <div class="mk-val" id="m-cpc-T">—</div>
          <div class="mk-portal-row">
            <span class="mk-portal-pill pill-sm">SM <b id="m-cpc-SM">—</b></span>
            <span class="mk-portal-pill pill-sml">SML <b id="m-cpc-SML">—</b></span>
            <span class="mk-portal-pill pill-nbp">NBP <b id="m-cpc-NBP">—</b></span>
          </div></div>
      </div>
    </div>

    <!-- 🔗 Funnel -->
    <div class="kpi-group">
      <div class="kpi-group-label"><span class="lbl-funnel">🔗 Funnel Metrics</span></div>
      <div class="mk-grid mk-grid-5">
        <div class="mk mk-funnel"><div class="mk-icon">🖱</div><div class="mk-lbl">Outbound Clicks</div>
          <div class="mk-val" id="m-ob-T">—</div>
          <div class="mk-portal-row">
            <span class="mk-portal-pill pill-sm">SM <b id="m-ob-SM">—</b></span>
            <span class="mk-portal-pill pill-sml">SML <b id="m-ob-SML">—</b></span>
            <span class="mk-portal-pill pill-nbp">NBP <b id="m-ob-NBP">—</b></span>
          </div></div>
        <div class="mk mk-funnel"><div class="mk-icon">🌐</div><div class="mk-lbl">LPV</div>
          <div class="mk-val" id="m-lpv-T">—</div>
          <div class="mk-portal-row">
            <span class="mk-portal-pill pill-sm">SM <b id="m-lpv-SM">—</b></span>
            <span class="mk-portal-pill pill-sml">SML <b id="m-lpv-SML">—</b></span>
            <span class="mk-portal-pill pill-nbp">NBP <b id="m-lpv-NBP">—</b></span>
          </div></div>
        <div class="mk mk-funnel"><div class="mk-icon">🛒</div><div class="mk-lbl">ATC</div>
          <div class="mk-val" id="m-atc-T">—</div>
          <div class="mk-portal-row">
            <span class="mk-portal-pill pill-sm">SM <b id="m-atc-SM">—</b></span>
            <span class="mk-portal-pill pill-sml">SML <b id="m-atc-SML">—</b></span>
            <span class="mk-portal-pill pill-nbp">NBP <b id="m-atc-NBP">—</b></span>
          </div></div>
        <div class="mk mk-funnel"><div class="mk-icon">📉</div><div class="mk-lbl">ATC Rate %</div>
          <div class="mk-val" id="m-atcrate-T">—</div>
          <div class="mk-portal-row">
            <span class="mk-portal-pill pill-sm">SM <b id="m-atcrate-SM">—</b></span>
            <span class="mk-portal-pill pill-sml">SML <b id="m-atcrate-SML">—</b></span>
            <span class="mk-portal-pill pill-nbp">NBP <b id="m-atcrate-NBP">—</b></span>
          </div></div>
        <div class="mk mk-funnel"><div class="mk-icon">🎯</div><div class="mk-lbl">LP CVR %</div>
          <div class="mk-val" id="m-lpcvr-T">—</div>
          <div class="mk-portal-row">
            <span class="mk-portal-pill pill-sm">SM <b id="m-lpcvr-SM">—</b></span>
            <span class="mk-portal-pill pill-sml">SML <b id="m-lpcvr-SML">—</b></span>
            <span class="mk-portal-pill pill-nbp">NBP <b id="m-lpcvr-NBP">—</b></span>
          </div></div>
      </div>
    </div>

    <!-- 🛒 Conversions -->
    <div class="kpi-group">
      <div class="kpi-group-label"><span class="lbl-conv">🛒 Conversions & Revenue</span></div>
      <div class="mk-grid mk-grid-5">
        <div class="mk mk-conv"><div class="mk-icon">✅</div><div class="mk-lbl">Purchases (Meta)</div>
          <div class="mk-val" id="m-pur-T">—</div>
          <div class="mk-portal-row">
            <span class="mk-portal-pill pill-sm">SM <b id="m-pur-SM">—</b></span>
            <span class="mk-portal-pill pill-sml">SML <b id="m-pur-SML">—</b></span>
            <span class="mk-portal-pill pill-nbp">NBP <b id="m-pur-NBP">—</b></span>
          </div></div>
        <div class="mk mk-conv"><div class="mk-icon">💰</div><div class="mk-lbl">Revenue (Meta ₹)</div>
          <div class="mk-val" id="m-rev-T">—</div>
          <div class="mk-portal-row">
            <span class="mk-portal-pill pill-sm">SM <b id="m-rev-SM">—</b></span>
            <span class="mk-portal-pill pill-sml">SML <b id="m-rev-SML">—</b></span>
            <span class="mk-portal-pill pill-nbp">NBP <b id="m-rev-NBP">—</b></span>
          </div></div>
        <div class="mk mk-conv"><div class="mk-icon">🎯</div><div class="mk-lbl">ROAS</div>
          <div class="mk-val" id="m-roas-T">—</div>
          <div class="mk-portal-row">
            <span class="mk-portal-pill pill-sm">SM <b id="m-roas-SM">—</b></span>
            <span class="mk-portal-pill pill-sml">SML <b id="m-roas-SML">—</b></span>
            <span class="mk-portal-pill pill-nbp">NBP <b id="m-roas-NBP">—</b></span>
          </div></div>
        <div class="mk mk-conv"><div class="mk-icon">📦</div><div class="mk-lbl">CPP (₹)</div>
          <div class="mk-val" id="m-cpp-T">—</div>
          <div class="mk-portal-row">
            <span class="mk-portal-pill pill-sm">SM <b id="m-cpp-SM">—</b></span>
            <span class="mk-portal-pill pill-sml">SML <b id="m-cpp-SML">—</b></span>
            <span class="mk-portal-pill pill-nbp">NBP <b id="m-cpp-NBP">—</b></span>
          </div></div>
        <div class="mk mk-conv"><div class="mk-icon">💳</div><div class="mk-lbl">Ad Spend (₹)</div>
          <div class="mk-val" id="m-spend-T">—</div>
          <div class="mk-portal-row">
            <span class="mk-portal-pill pill-sm">SM <b id="m-spend-SM">—</b></span>
            <span class="mk-portal-pill pill-sml">SML <b id="m-spend-SML">—</b></span>
            <span class="mk-portal-pill pill-nbp">NBP <b id="m-spend-NBP">—</b></span>
          </div></div>
      </div>
    </div>

    <!-- ── Full Comparison Table ── -->
    <div class="meta-table-wrap">
      <div class="sec-ttl">📋 Full KPI Comparison — All Portals</div>
      <table class="meta-tbl">
        <thead>
          <tr>
            <th class="h-metric">KPI</th>
            <th class="h-sm">SM</th>
            <th class="h-sml">SML</th>
            <th class="h-nbp">NBP</th>
            <th class="h-total">Total</th>
          </tr>
        </thead>
        <tbody id="meta-tbl-body">
          <tr><td colspan="5" style="text-align:center;color:#9ca3af;padding:20px">Select a date to load data</td></tr>
        </tbody>
      </table>
    </div>
  </div>

  <div class="charts-row">
    <div class="chart-card"><div class="chart-title">📈 ROAS Trend</div><canvas id="c1" height="160"></canvas></div>
    <div class="chart-card"><div class="chart-title">📦 Orders by Date</div><canvas id="c2" height="160"></canvas></div>
    <div class="chart-card"><div class="chart-title">📡 CPM by Portal</div><canvas id="c3" height="160"></canvas></div>
  </div>
  <div class="tables-row">
    <div class="tbl-card full">
      <div class="sec-ttl">👥 New vs Returning  <span style="color:#6b7280;font-weight:400;font-size:11px">— with 7-day average and today vs avg delta</span></div>
      <table><thead><tr><th>Metric</th>{nr_th_cols()}</tr></thead><tbody>{nr_rows()}</tbody></table>
    </div>
  </div>
  <div class="tables-row">
    <div class="tbl-card">
      <div class="sec-ttl">🎨 Creative Performance</div>
      <table><thead><tr><th>Type</th><th>ROAS</th><th>Spend (₹)</th></tr></thead><tbody>{creative_rows()}</tbody></table>
    </div>
    <div class="tbl-card">
      <div class="sec-ttl">🔴 Today Live  <span style="color:#6b7280;font-weight:400;font-size:11px">— refreshes every 2 hrs</span></div>
      {('<table><thead><tr><th>Metric</th><th>Value</th></tr></thead><tbody>'
        f'<tr><td>Source tab</td><td>{LIVE.get("tab_name","—")}</td></tr>'
        f'<tr><td>Last update</td><td>{LIVE.get("updated","—")}</td></tr>'
        f'<tr><td>Camps with spend today</td><td><b>{LIVE.get("total_camps","—")}</b></td></tr>'
        f'<tr><td>Total spend today</td><td><b>₹{LIVE.get("total_spend","—")}</b></td></tr>'
        f'<tr><td>% spend below 1.0x ROAS</td><td><b style="color:#a3260a">{LIVE.get("pct_below_1","—")}</b></td></tr>'
        f'<tr><td>% spend above 1.5x ROAS</td><td><b style="color:#0d6e3a">{LIVE.get("pct_above_1_5","—")}</b></td></tr>'
        '</tbody></table>') if LIVE else '<p style="color:#888;font-size:11px">No live tab found — has today_live_report run today?</p>'}
    </div>
  </div>
  <div class="tables-row">
    <div class="tbl-card full">
      <div class="sec-ttl">🏪 Sales Block Opening Report</div>
      <table><thead><tr><th>TOF</th><th>Remarks</th><th>NBP</th><th>SM</th><th>SML</th><th>Grand Total</th></tr></thead>
      <tbody>{sb_rows()}</tbody></table>
    </div>
  </div>

  <!-- ═══ C1-C6 Cohort Pending % — Master file (vs editable targets) ═══ -->
  <div class="tables-row">
    <div class="tbl-card full">
      <div class="sec-ttl">🎯 C1-C6 Cohort Pending %  <span style="color:#6b7280;font-weight:400;font-size:11px">— Master file · per-portal · actuals vs targets (lower is better — edit targets in GHA sheet → 'C1-C6 Targets' tab)</span></div>
      <table><thead><tr><th>SM Cohort</th>{th_cols()}<th>Target</th><th>vs Target</th></tr></thead><tbody>{c16_rows(D['c16_master_sm'], 'SM')}</tbody></table>
      <div style="height:8px"></div>
      <table><thead><tr><th>SML Cohort</th>{th_cols()}<th>Target</th><th>vs Target</th></tr></thead><tbody>{c16_rows(D['c16_master_sml'], 'SML')}</tbody></table>
      <div style="height:8px"></div>
      <table><thead><tr><th>NBP Cohort</th>{th_cols()}<th>Target</th><th>vs Target</th></tr></thead><tbody>{c16_rows(D['c16_master_nbp'], 'NBP')}</tbody></table>
    </div>
  </div>
  <div class="prods">
    <div class="tbl-card"><div class="sec-ttl">🛍️ Products — SM · 4-Apr</div>
      <table><thead><tr><th>Product</th><th>ROAS</th><th>Budget</th></tr></thead><tbody>{prod_rows('sm_prods')}</tbody></table></div>
    <div class="tbl-card"><div class="sec-ttl">🛍️ Products — SML · 4-Apr</div>
      <table><thead><tr><th>Product</th><th>ROAS</th><th>Budget</th></tr></thead><tbody>{prod_rows('sml_prods')}</tbody></table></div>
    <div class="tbl-card"><div class="sec-ttl">🛍️ Products — NBP · 4-Apr</div>
      <table><thead><tr><th>Product</th><th>ROAS</th><th>Budget</th></tr></thead><tbody>{prod_rows('nbp_prods')}</tbody></table></div>
  </div>
</div>
<footer>🤖 Antriksh · NTN Dashboard · {D['updated']}</footer>
<script>
const KPI = {json.dumps(kpi)};
const KPI_DAILY = {json.dumps({dl: kpi_daily_for_dl(dl) for dl in DL})};
let currentDate = null;

// All KPI fields mapping: id → {{ key, pct, roas, label }}
const META_FIELDS = [
  // Media
  {{g:'media', id:'imp',       key:'impressions', label:'Impressions'}},
  {{g:'media', id:'reach',     key:'reach',       label:'Reach'}},
  {{g:'media', id:'freq',      key:'frequency',   label:'Frequency'}},
  {{g:'media', id:'cpm',       key:'cpm',         label:'CPM (₹)'}},
  {{g:'media', id:'cpr',       key:'cpr',         label:'CPR/1L Reach (₹)'}},
  // Video
  {{g:'video', id:'thumbstop', key:'thumbstop',   label:'Thumbstop %',  pct:true}},
  {{g:'video', id:'hold',      key:'hold_rate',   label:'Hold Rate %',  pct:true}},
  {{g:'video', id:'ctr',       key:'ctr',         label:'CTR %',        pct:true}},
  {{g:'video', id:'cpc',       key:'cpc',         label:'CPC (₹)'}},
  // Funnel
  {{g:'funnel',id:'ob',        key:'ob',          label:'Outbound Clicks'}},
  {{g:'funnel',id:'lpv',       key:'lpv',         label:'LPV'}},
  {{g:'funnel',id:'atc',       key:'atc',         label:'ATC'}},
  {{g:'funnel',id:'atcrate',   key:'atc_rate',    label:'ATC Rate %',   pct:true}},
  {{g:'funnel',id:'lpcvr',     key:'lp_cvr',      label:'LP CVR %',     pct:true}},
  // Conv
  {{g:'conv',  id:'pur',       key:'purchases',   label:'Purchases'}},
  {{g:'conv',  id:'rev',       key:'revenue',     label:'Revenue (₹)'}},
  {{g:'conv',  id:'roas',      key:'roas',        label:'ROAS',         roas:true}},
  {{g:'conv',  id:'cpp',       key:'cpp',         label:'CPP (₹)'}},
  {{g:'conv',  id:'spend',     key:'spend',       label:'Ad Spend (₹)'}},
];

function fmtVal(raw, field) {{
  if (!raw || raw === '—') return '—';
  const n = parseFloat(raw);
  if (isNaN(n)) return raw;
  if (field.pct)  return n.toFixed(2) + '%';
  if (field.roas) return n.toFixed(2) + 'x';
  return raw;
}}

function roasClass(val) {{
  const n = parseFloat(val);
  if (isNaN(n)) return 'val-neu';
  return n >= 2.0 ? 'val-good' : n >= 1.5 ? 'val-warn' : 'val-bad';
}}

function setMetaKPIs(dateLabel) {{
  const dayData = KPI_DAILY[dateLabel];
  const portals = ['SM','SML','NBP','Total'];

  // Update label
  const lbl = document.getElementById('meta-date-label');
  if (lbl) lbl.textContent = dayData ? 'Data for ' + dateLabel : 'No Meta data for ' + dateLabel;

  // Update gradient cards (Total in main val, SM/SML/NBP in pills)
  META_FIELDS.forEach(f => {{
    ['T','SM','SML','NBP'].forEach(p => {{
      const el = document.getElementById('m-' + f.id + '-' + p);
      if (!el) return;
      const portal = p === 'T' ? 'Total' : p;
      const k = dayData ? dayData[portal] : null;
      const raw = k ? k[f.key] : null;
      el.textContent = fmtVal(raw, f);
    }});
  }});

  // Build comparison table
  const tbody = document.getElementById('meta-tbl-body');
  if (!tbody) return;
  if (!dayData) {{
    tbody.innerHTML = '<tr><td colspan="5" style="text-align:center;color:#9ca3af;padding:20px">No Meta KPI data for this date</td></tr>';
    return;
  }}

  const groups = [
    {{label:'📊 Media', ids:['imp','reach','freq','cpm','cpr']}},
    {{label:'🎥 Video & Engagement', ids:['thumbstop','hold','ctr','cpc']}},
    {{label:'🔗 Funnel', ids:['ob','lpv','atc','atcrate','lpcvr']}},
    {{label:'🛒 Conversions', ids:['pur','rev','roas','cpp','spend']}},
  ];

  let html = '';
  groups.forEach(grp => {{
    html += `<tr class="group-hdr"><td colspan="5">${{grp.label}}</td></tr>`;
    grp.ids.forEach(fid => {{
      const field = META_FIELDS.find(f => f.id === fid);
      if (!field) return;
      html += '<tr>';
      html += `<td>${{field.label}}</td>`;
      ['SM','SML','NBP','Total'].forEach(p => {{
        const k = dayData[p];
        const raw = k ? k[field.key] : null;
        const val = fmtVal(raw, field);
        const cls = field.roas ? roasClass(raw) : 'val-neu';
        html += `<td class="${{cls}}">${{val}}</td>`;
      }});
      html += '</tr>';
    }});
  }});
  tbody.innerHTML = html;
}}

function setDate(d, btn) {{
  document.querySelectorAll('.date-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  currentDate = d;
  const k = KPI[d]; if (!k) return;
  document.getElementById('v-o').textContent = k.o;
  document.getElementById('v-r').textContent = k.r;
  document.getElementById('v-s').textContent = k.s;
  document.getElementById('v-roas').textContent = k.roas;
  const v = k.rv;
  document.getElementById('v-badge').innerHTML = v>=2.5?'<span class="badge bg">🔥 Excellent</span>':v>=2.0?'<span class="badge bg">✅ On Target</span>':v>0?'<span class="badge bo">⚠️ Below Target</span>':'<span class="badge" style="background:#e2e8f0;color:#64748b">No Data</span>';
  setMetaKPIs(d);
}}
window.addEventListener('DOMContentLoaded', function() {{
  const btns = document.querySelectorAll('.date-btn');
  if (btns.length > 1) btns[btns.length - 2].click();
  else if (btns.length > 0) btns[0].click();
}});
function drawBar(id,labels,datasets,opts={{}}){{
  const canvas=document.getElementById(id);if(!canvas)return;
  const ctx=canvas.getContext('2d');const dpr=window.devicePixelRatio||1;
  canvas.width=(canvas.offsetWidth||400)*dpr;canvas.height=(opts.h||160)*dpr;ctx.scale(dpr,dpr);
  const W=canvas.width/dpr,H=opts.h||160,pad={{t:20,r:20,b:35,l:50}},cw=W-pad.l-pad.r,ch=H-pad.t-pad.b;
  ctx.clearRect(0,0,W,H);
  const allV=datasets.flatMap(d=>d.data.filter(v=>v!=null));if(!allV.length)return;
  const maxV=Math.max(...allV)*1.2,minV=opts.line?Math.min(...allV)*0.85:0,range=maxV-minV||1;
  for(let i=0;i<=4;i++){{const y=pad.t+ch*(1-i/4);ctx.strokeStyle='#eef2ff';ctx.lineWidth=1;ctx.beginPath();ctx.moveTo(pad.l,y);ctx.lineTo(pad.l+cw,y);ctx.stroke();ctx.fillStyle='#9ca3af';ctx.font='10px Segoe UI';ctx.textAlign='right';ctx.fillText(((minV+range*i/4)).toFixed(opts.dec||0),pad.l-4,y+4);}}
  const n=labels.length,grpW=cw/n,barW=grpW*0.65/datasets.length;
  datasets.forEach((ds,di)=>{{ctx.fillStyle=ds.color;ctx.strokeStyle=ds.color;
    if(opts.line){{ctx.beginPath();ctx.lineWidth=2.5;ctx.lineJoin='round';let first=true;ds.data.forEach((v,i)=>{{if(v==null)return;const x=pad.l+grpW*i+grpW/2,y=pad.t+ch*(1-(v-minV)/range);first?(ctx.moveTo(x,y),first=false):ctx.lineTo(x,y);}});ctx.stroke();ds.data.forEach((v,i)=>{{if(v==null)return;const x=pad.l+grpW*i+grpW/2,y=pad.t+ch*(1-(v-minV)/range);ctx.beginPath();ctx.arc(x,y,4,0,Math.PI*2);ctx.fill();}});}}
    else{{ds.data.forEach((v,i)=>{{if(v==null)return;const x=pad.l+grpW*i+(grpW-barW*datasets.length)/2+di*barW,bh=ch*(v-minV)/range,y=pad.t+ch-bh;ctx.beginPath();ctx.roundRect?ctx.roundRect(x,y,barW-2,bh,3):ctx.rect(x,y,barW-2,bh);ctx.fill();}});}}
  }});
  ctx.fillStyle='#374151';ctx.font='bold 11px Segoe UI';ctx.textAlign='center';labels.forEach((l,i)=>ctx.fillText(l,pad.l+grpW*i+grpW/2,H-8));
  if(datasets.length>1){{let lx=pad.l;datasets.forEach(ds=>{{ctx.fillStyle=ds.color;ctx.fillRect(lx,5,10,10);ctx.fillStyle='#374151';ctx.font='9px Segoe UI';ctx.textAlign='left';ctx.fillText(ds.label,lx+13,14);lx+=ds.label.length*6+26;}});}}
}}
const L={json.dumps(DL)};
drawBar('c1',L,[{{data:{ch_arr('roas')},color:'#00a878',label:'ROAS'}}],{{line:true,dec:2,h:160}});
drawBar('c2',L,[{{data:{ord_arr()},color:'#1a3d7c',label:'Orders'}}],{{h:160}});
drawBar('c3',L,[{{data:{ch_arr('sm_cpm')},color:'#1a3d7c',label:'SM'}},{{data:{ch_arr('sml_cpm')},color:'#00a878',label:'SML'}},{{data:{ch_arr('nbp_cpm')},color:'#f5c518',label:'NBP'}}],{{h:160}});
</script>
</body></html>"""

# Save HTML to out/ (committed as build artifact)
_html_path = str(OUT_DIR / 'ntn_filtered.html')
with open(_html_path, 'w') as f:
    f.write(html)

# EC2 deploy is opt-in — the original script on EC2 is still the live source.
# To deploy from this copy, set ENABLE_EC2_DEPLOY=1 and provide EC2_SSH_KEY +
# EC2_HOST env vars. Otherwise we just build the HTML artifact.
if os.environ.get('ENABLE_EC2_DEPLOY') == '1' and SSH_KEY and EC2:
    subprocess.run(
        ['scp', '-i', SSH_KEY, _html_path, f'{EC2}:/home/ec2-user/ntn_filtered.html'],
        capture_output=True, text=True
    )
    subprocess.run(
        ['ssh', '-i', SSH_KEY, EC2, 'sudo cp /home/ec2-user/ntn_filtered.html /usr/share/nginx/html/ntn_dashboard.html'],
        capture_output=True, text=True
    )
    print(f"[{datetime.now().strftime('%H:%M:%S')}] ✅ Dashboard rebuilt & deployed! Dates: {DL}")
else:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] ✅ Dashboard built (deploy skipped): {_html_path} — Dates: {DL}")
