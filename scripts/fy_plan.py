#!/usr/bin/env python3
"""
FY Plan 2026-27 — roadmap tab in the GHA reports sheet.

Writes a single tab "📅 FY Plan 2026-27" with:
  1. Assumption block (editable values at top)
  2. Site targets (SM / NBP / SML — today vs target vs multiplier)
  3. Phase plan (1A scale-up → 1B cleanslate → 1C re-scale → 2 sustain)
  4. Monthly targets (12 rows, floor + stretch + actuals placeholders)
  5. Daily projection (FY day 1 → 365, phase tag, target curve)
  6. Cleanslate readiness for SM (current snapshot from SM tracker)

Usage:
    python3 scripts/fy_plan.py

Reads .env (META/SHEET creds) and config/sheets.env (REPORTS_SHEET_ID).
Pattern: delete + recreate tab (per architecture; never clear + rewrite).
"""
import os, datetime
from dotenv import load_dotenv
import gspread
from google.oauth2.service_account import Credentials

load_dotenv()

# --- inputs ----------------------------------------------------------------
TAB = '📅 FY Plan 2026-27'

FY_START = datetime.date(2026, 4, 1)
FY_END   = datetime.date(2027, 3, 31)
FY_DAYS  = (FY_END - FY_START).days + 1   # 365

TARGET_FY_CR    = 400      # ₹ cr
DAILY_ORDERS    = 13000    # 7000 SM + 3000 SML + 3000 NBP
AOV             = 950      # ₹
TARGET_ROAS     = 2.30

DAILY_REV_LAC   = DAILY_ORDERS * AOV / 1e5     # 118.75 lac
DAILY_REV_CR    = DAILY_REV_LAC / 100          # 1.1875 cr
STRETCH_FY_CR   = DAILY_REV_CR * FY_DAYS       # ~433 cr
DAILY_SPEND_LAC = DAILY_REV_LAC / TARGET_ROAS  # ~51.6 lac
FLOOR_DAILY_LAC = TARGET_FY_CR * 100 / FY_DAYS # 109.59 lac/day to hit ₹400 cr exactly

SITES = [
    # name, today_orders, target_orders, today_rev_lac
    ('SM',  1500, 7000, 15.00),
    ('NBP',  350, 3000,  3.00),
    ('SML',  325, 3000,  2.80),
]

# Months that have closed out — locked at known monthly revenue (₹ cr).
# Floor target for these months = locked value (no longer aspirational).
# Floor target for remaining months = (₹400 cr − sum of locked) / months_remaining.
LOCKED_MONTHLY_REV_CR = {
    'Apr 2026': 5.20,    # April closed at ₹5.20 cr (target was missed)
}

def _parse_month_label(lbl):
    """'Apr 2026' -> (year, month) tuple."""
    return datetime.datetime.strptime(lbl, '%b %Y').date().replace(day=1)
def _days_in_month(d):
    nxt = (d.replace(day=28) + datetime.timedelta(days=4)).replace(day=1)
    return (nxt - d).days

_locked_total     = sum(LOCKED_MONTHLY_REV_CR.values())
_remaining_months = 12 - len(LOCKED_MONTHLY_REV_CR)
_remaining_target = TARGET_FY_CR - _locked_total
RECALIB_MONTH_CR  = _remaining_target / _remaining_months   # cr/month for non-locked
_locked_days      = sum(_days_in_month(_parse_month_label(lbl)) for lbl in LOCKED_MONTHLY_REV_CR)
RECALIB_DAILY_LAC = _remaining_target * 100 / (FY_DAYS - _locked_days)   # lac/day post-locked

# Cleanslate snapshot — captured from SM 27 Apr 26 analysis
CLEANSLATE_SNAPSHOT = [
    # cutoff, window, n_survive, pct_camps, surviving_budget, pct_budget, avg_roas
    (1.50, '7D', 28, '23.1%', 219896, '19.0%', 1.84),
    (1.75, '7D', 16, '13.2%',  92071,  '7.9%', 2.04),
    (2.00, '7D', 10,  '8.3%',  31626,  '2.7%', 2.43),
    (2.30, '7D',  7,  '5.8%',  24317,  '2.1%', 2.51),
    (2.30, '1D',  5,  '4.1%',  71562,  '6.2%', 3.13),
]

# --- connect ---------------------------------------------------------------
creds = Credentials.from_service_account_file(
    os.environ['GOOGLE_SERVICE_ACCOUNT_FILE'],
    scopes=['https://www.googleapis.com/auth/spreadsheets'],
)
sh = gspread.authorize(creds).open_by_key(os.environ['REPORTS_SHEET_ID'])


# --- compute cleanslate budget per date, split digital vs non-digital ------
DIGITAL_KEYWORDS = ('astro', 'destiny', 'bot')   # case-insensitive substring on campaign name
DIGITAL_ROAS_CUT     = 0.80
NON_DIGITAL_ROAS_CUT = 1.50

def cleanslate_by_date(sh):
    """For each SM/SML/NBP daily tab, compute cleanslate budget split into
    digital / non-digital, using two rules together:

      1. Today's reading (max-days-window: 7D → 3D → 2D → 1D fallback) ≤ cutoff
      2. Historical max ROAS across all PRIOR daily tabs (any window) ≤ cutoff

    A campaign that ever performed above cutoff on a prior day is NOT cleanslated
    even if today is weak — the user's salvage rule.

    Cutoffs:  digital ≤ 0.80   non-digital ≤ 1.50."""
    def f(s):
        try: return float(str(s).replace(',','').replace('₹','').replace('%','').strip() or 0)
        except: return 0.0

    # Pass 1: collect every daily tab's parsed rows, indexed by date
    by_date = {}   # date -> list of (campaign_id, name, status, budget, r1, r2, r3, r7)
    for ws in sh.worksheets():
        t = ws.title
        if not (t.startswith('SM ') or t.startswith('SML ') or t.startswith('NBP ')):
            continue
        try:
            d = datetime.datetime.strptime(t.split(' ', 1)[1], '%d %b %y').date()
        except (ValueError, IndexError):
            continue
        data = ws.get_all_values()
        if len(data) < 2: continue
        H = [h.lower() for h in data[0]]
        try:
            i_cid    = next(i for i,h in enumerate(H) if h == 'campaign_id')
            i_camp   = next(i for i,h in enumerate(H) if h == 'campaign name')
            i_status = next(i for i,h in enumerate(H) if h == 'status')
            i_budget = next(i for i,h in enumerate(H) if h == 'budget')
            i_r1 = next(i for i,h in enumerate(H) if h.startswith('roas 1'))
            i_r2 = next(i for i,h in enumerate(H) if h.startswith('roas 2'))
            i_r3 = next(i for i,h in enumerate(H) if h.startswith('roas 3'))
            i_r7 = next(i for i,h in enumerate(H) if h.startswith('roas 7'))
        except StopIteration:
            continue
        rows_for_day = []
        for row in data[1:]:
            if len(row) <= max(i_cid, i_camp, i_status, i_budget, i_r1, i_r2, i_r3, i_r7):
                continue
            cid = row[i_cid].strip()
            if not cid: continue
            rows_for_day.append((
                cid, row[i_camp].strip(), row[i_status].strip().lower(),
                f(row[i_budget]),
                f(row[i_r1]), f(row[i_r2]), f(row[i_r3]), f(row[i_r7]),
            ))
        by_date.setdefault(d, []).extend(rows_for_day)

    # Pass 2: walk dates chronologically, maintain rolling historical max per cid
    out = {}
    historical_max = {}   # cid -> max ROAS in any window seen on any PRIOR date
    for eval_date in sorted(by_date.keys()):
        bucket = out.setdefault(eval_date, {'digital': 0.0, 'nondigital': 0.0})
        for cid, name, status, budget, r1, r2, r3, r7 in by_date[eval_date]:
            if status not in ('active', 'running'): continue
            today_roas = r7 if r7 > 0 else r3 if r3 > 0 else r2 if r2 > 0 else r1
            if today_roas == 0: continue   # no signal yet
            hist_max = historical_max.get(cid, 0.0)
            is_digital = any(k in name.lower() for k in DIGITAL_KEYWORDS)
            cutoff = DIGITAL_ROAS_CUT if is_digital else NON_DIGITAL_ROAS_CUT
            # Cleanslate eligible only if BOTH today and history are below cutoff
            if today_roas <= cutoff and hist_max <= cutoff:
                if is_digital: bucket['digital']    += budget
                else:          bucket['nondigital'] += budget
        # NOW fold this date's data into historical_max (so future dates see it)
        for cid, _, _, _, r1, r2, r3, r7 in by_date[eval_date]:
            best = max(r1, r2, r3, r7)
            if best > historical_max.get(cid, 0.0):
                historical_max[cid] = best
    return out

print('  reading per-day cleanslate budgets from existing tracker tabs...')
CLEANSLATE_BY_DATE = cleanslate_by_date(sh)
print(f'  found data for {len(CLEANSLATE_BY_DATE)} day(s)')


# --- compute actual daily revenue per date ---------------------------------
def actual_revenue_by_date(sh):
    """For each SM/SML/NBP daily tab, sum (spend × 1D-ROAS) across rows.
    1D ROAS uses 1d_click attribution = today's direct conversions.
    Returns {date: total_rev_in_rupees}."""
    def f(s):
        try: return float(str(s).replace(',','').replace('₹','').replace('%','').strip() or 0)
        except: return 0.0
    out = {}
    for ws in sh.worksheets():
        t = ws.title
        if not (t.startswith('SM ') or t.startswith('SML ') or t.startswith('NBP ')):
            continue
        try:
            d = datetime.datetime.strptime(t.split(' ', 1)[1], '%d %b %y').date()
        except (ValueError, IndexError):
            continue
        data = ws.get_all_values()
        if len(data) < 2: continue
        H = [h.lower() for h in data[0]]
        try:
            i_spend = next(i for i,h in enumerate(H) if 'spent' in h or h == 'spend')
            i_r1    = next(i for i,h in enumerate(H) if h.startswith('roas 1'))
        except StopIteration:
            continue
        rev = 0.0
        for row in data[1:]:
            if len(row) <= max(i_spend, i_r1): continue
            s = f(row[i_spend]); r = f(row[i_r1])
            if s > 0 and r > 0:
                rev += s * r
        out[d] = out.get(d, 0.0) + rev
    return out

print('  reading per-day actual revenue from tracker tabs...')
ACTUAL_REV_BY_DATE = actual_revenue_by_date(sh)
print(f'  found revenue data for {len(ACTUAL_REV_BY_DATE)} day(s)')

# --- build rows ------------------------------------------------------------
rows = []
def r(*cells): rows.append(list(cells))
def blank(): rows.append([])

today = datetime.date.today()
fy_day_today = (today - FY_START).days + 1

r('📅 FY Plan 2026-27 — Roadmap to ₹400 cr')
r(f'Generated {today.isoformat()} by scripts/fy_plan.py · re-run to refresh')
blank()

# 1. Assumptions
r('ASSUMPTIONS')
r('Target FY revenue (cr)',         TARGET_FY_CR)
r('Target daily orders',            DAILY_ORDERS)
r('Target AOV (₹)',                 AOV)
r('Target ROAS',                    TARGET_ROAS)
r('Implied daily revenue (lac)',    round(DAILY_REV_LAC, 2))
r('Implied daily revenue (cr)',     round(DAILY_REV_CR, 4))
r('Implied annual revenue (cr)',    round(STRETCH_FY_CR, 2))
r('Buffer over ₹400 cr (cr)',       round(STRETCH_FY_CR - TARGET_FY_CR, 2))
r('Implied daily spend (lac)',      round(DAILY_SPEND_LAC, 2))
r('Floor daily revenue to hit ₹400 cr (lac)', round(FLOOR_DAILY_LAC, 2))
r('FY days', FY_DAYS)
r('FY day today',  f'{fy_day_today} of {FY_DAYS} ({FY_DAYS - fy_day_today} remaining)')
blank()

# 2. Site targets
r('SITE TARGETS')
r('Site', 'Today orders', 'Target orders', 'Multiplier',
  'Today rev (lac)', 'Target rev (lac)', 'Target spend (lac)')
tot_today_o = tot_tgt_o = tot_today_rev = tot_tgt_rev = 0.0
for name, today_o, tgt_o, today_rev in SITES:
    tgt_rev   = tgt_o * AOV / 1e5
    tgt_spend = tgt_rev / TARGET_ROAS
    r(name, today_o, tgt_o, f'{tgt_o/today_o:.2f}x',
      round(today_rev, 2), round(tgt_rev, 2), round(tgt_spend, 2))
    tot_today_o += today_o; tot_tgt_o += tgt_o
    tot_today_rev += today_rev; tot_tgt_rev += tgt_rev
r('TOTAL', tot_today_o, tot_tgt_o, f'{tot_tgt_o/tot_today_o:.2f}x',
  round(tot_today_rev, 2), round(tot_tgt_rev, 2), round(tot_tgt_rev/TARGET_ROAS, 2))
blank()

# 3. Phase plan
r('PHASE PLAN  (Day 1 = today; SM-driven scale + cleanslate)')
r('Phase', 'Days', 'Trigger', 'Action', 'End-state daily total')
r('1A — Scale-up',       '1–9',     'Start',
  'SM +₹1 lac net/day (₹2 add, ₹1 close); NBP +₹40k/day; SML steady',
  'Total spend ≈ ₹25 lac')
r('1B — Cleanslate',     '10',       'Total spend = ₹25 lac',
  'Close all SM camps with ROAS < 2.3',
  '≈57% of budget surviving at ≥2.3 ROAS (target)')
r('1C — Re-scale',       '11–25',    'Cleanslate done',
  '11% daily compound on total budget',
  'Total spend ≈ ₹45 lac (revenue ≈ ₹103 lac/day at 2.3 ROAS)')
r('2 — Sustain & grow',  '26–365',   'Total spend = ₹45 lac',
  '10% weekly (glide-path; cap at FY trajectory)',
  'Reach ₹1.19 cr revenue/day = ₹433 cr/year')
blank()

# 4. Monthly targets — simplified to Target / Achieved / Gap
r('MONTHLY TARGETS')
r('Month', 'Days',
  'Target Rev (cr)', 'Target Orders',
  'Achieved Rev (cr)', 'Achieved Orders',
  'Gap (cr)')

m = FY_START
cum_target_m = cum_actual_m = 0.0
while m <= FY_END:
    nxt = (m.replace(day=28) + datetime.timedelta(days=4)).replace(day=1)
    days = (min(nxt, FY_END + datetime.timedelta(days=1)) - m).days
    label = m.strftime('%b %Y')
    is_locked = label in LOCKED_MONTHLY_REV_CR
    target = LOCKED_MONTHLY_REV_CR[label] if is_locked else RECALIB_MONTH_CR
    cum_target_m += target
    target_orders = round(target * 1e7 / AOV)            # ₹cr → orders @AOV950
    if is_locked:
        actual = LOCKED_MONTHLY_REV_CR[label]
        cum_actual_m += actual
        actual_cell        = round(actual, 2)
        actual_orders_cell = round(actual * 1e7 / AOV)
        gap_cell           = round(cum_actual_m - cum_target_m, 2)
    else:
        actual_cell = actual_orders_cell = gap_cell = ''
    r(label, days,
      round(target, 2), target_orders,
      actual_cell, actual_orders_cell,
      gap_cell)
    m = nxt
blank()

# 5. Daily projection — simplified to Target / Achieved / Gap
r('DAILY PROJECTION  (FY Day 1 = 1 Apr 2026)')
r('FY Day', 'Date', 'DOW', 'Phase',
  'Target Rev (lac)', 'Target Orders',
  'Achieved Rev (lac)', 'Achieved Orders',
  'Gap to-date (cr)',
  'Cleanslate (lac)')

# Phase 1A starts on the day this script is run (the "plan Day 1")
plan_day1_fy = fy_day_today  # FY day index when our action plan begins
def phase_for(fy_day):
    plan_day = fy_day - plan_day1_fy + 1
    if plan_day < 1: return ''
    if plan_day <= 9:  return '1A Scale-up'
    if plan_day == 10: return '1B Cleanslate'
    if plan_day <= 25: return '1C Re-scale'
    return '2 Sustain & grow'

cum_target_lac = cum_actual_lac = 0.0
for fy_day in range(1, FY_DAYS + 1):
    d = FY_START + datetime.timedelta(days=fy_day - 1)
    month_label = d.strftime('%b %Y')
    if month_label in LOCKED_MONTHLY_REV_CR:
        target_lac = LOCKED_MONTHLY_REV_CR[month_label] * 100 / _days_in_month(d.replace(day=1))
    else:
        target_lac = RECALIB_DAILY_LAC
    cum_target_lac += target_lac
    target_orders = round(target_lac * 1e5 / AOV)

    actual_rev = ACTUAL_REV_BY_DATE.get(d)
    if actual_rev is None:
        actual_cell = actual_orders_cell = gap_cell = ''
    else:
        actual_lac = actual_rev / 1e5
        cum_actual_lac    += actual_lac
        actual_cell        = round(actual_lac, 2)
        actual_orders_cell = round(actual_rev / AOV)
        gap_cell           = round((cum_actual_lac - cum_target_lac) / 100, 2)

    cs = CLEANSLATE_BY_DATE.get(d)
    cs_cell = round((cs['digital'] + cs['nondigital']) / 1e5, 2) if cs else ''

    r(fy_day, d.isoformat(), d.strftime('%a'), phase_for(fy_day),
      round(target_lac, 2), target_orders,
      actual_cell, actual_orders_cell,
      gap_cell,
      cs_cell)
blank()

# 6. Cleanslate readiness
r('CLEANSLATE READINESS — SM (snapshot from SM 27 Apr 26)')
r('ROAS cutoff', 'Window', '# camps survive', '% camps',
  'Surviving budget (₹)', '% budget', 'Avg ROAS of survivors')
for row in CLEANSLATE_SNAPSHOT:
    r(*row)
blank()
r('Target before pulling cleanslate trigger:',
  '≈57% of ₹20 lac at ≥2.3 ROAS = ≈₹11.4 lac surviving (to hold revenue parity)')
r('Today (SM only):', '~6% of budget at ≥2.3 ROAS — significant mix-improvement work needed first.')

# --- write -----------------------------------------------------------------
existing = {w.title: w for w in sh.worksheets()}
if TAB in existing:
    sh.del_worksheet(existing[TAB])
ws = sh.add_worksheet(title=TAB, rows=max(500, len(rows) + 50), cols=12)
ws.update(values=rows, range_name='A1', value_input_option='USER_ENTERED')

# Light formatting: bold the title and section headers; freeze top row
ws.format('A1', {'textFormat': {'bold': True, 'fontSize': 14}})
section_rows = []
for i, row in enumerate(rows, start=1):
    if not row: continue
    cell = str(row[0])
    if cell in ('ASSUMPTIONS','SITE TARGETS','MONTHLY TARGETS',
                'DAILY PROJECTION  (FY Day 1 = 1 Apr 2026)',
                'PHASE PLAN  (Day 1 = today; SM-driven scale + cleanslate)',
                'CLEANSLATE READINESS — SM (snapshot from SM 27 Apr 26)'):
        section_rows.append(i)
for i in section_rows:
    ws.format(f'A{i}', {'textFormat': {'bold': True}, 'backgroundColor': {'red':0.9,'green':0.9,'blue':0.95}})

ws.freeze(rows=2)

print(f'OK: created tab "{TAB}"')
print(f'    {len(rows)} rows written')
print(f'    https://docs.google.com/spreadsheets/d/{os.environ["REPORTS_SHEET_ID"]}/edit#gid={ws.id}')
