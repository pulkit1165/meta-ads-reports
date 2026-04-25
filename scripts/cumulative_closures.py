#!/usr/bin/env python3
"""
Cumulative Closures Report — across all 🔴 Closing tabs in the sheet.

Tab: 📈 Cumulative Closures  (delete + recreate every run)

For each day where a closing tab exists, pulls the "Total Closed Since 10 AM"
section and aggregates:
  - # camps closed that day
  - Budget freed by those closures
  - Breakdown by Type (Sales / Retarget / TOF) where available

Plus a grand-total row across all days.

Usage:
  python3 cumulative_closures.py
"""

import os, sys, re, argparse
from datetime import datetime
from collections import defaultdict
from pathlib import Path
from zoneinfo import ZoneInfo

import gspread
from google.oauth2.service_account import Credentials
from dotenv import load_dotenv

# ── Path setup ────────────────────────────────────────────────────────────────
_REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_REPO_ROOT / '.env')

SA_FILE  = os.environ.get('GOOGLE_SERVICE_ACCOUNT_FILE') or str(_REPO_ROOT / 'google-service-account.json')
SHEET_ID = os.environ.get('REPORTS_SHEET_ID') or '1hJ3IS2VDtTAEyyJIV__jvts9CMQdYhyxKAfWKtrkUH4'
SCOPES   = ['https://www.googleapis.com/auth/spreadsheets']
IST      = ZoneInfo('Asia/Kolkata')

CLOSING_TAB_RE = re.compile(r'🔴\s+Closing\s+(\d{1,2})\s+(\w{3})\s+(\d{2,4})', re.IGNORECASE)
TOTAL_HEADER_RE = re.compile(r'TOTAL CLOSED SINCE 10\s*AM', re.IGNORECASE)


def parse_money(s):
    """'₹12,345' or '12345' or '₹0' → int."""
    if not s: return 0
    s = str(s).replace('₹', '').replace(',', '').strip()
    try: return int(float(s))
    except (ValueError, TypeError): return 0


def parse_date_from_tab(tab_title):
    m = CLOSING_TAB_RE.search(tab_title)
    if not m: return None
    day, mon, yr = m.group(1), m.group(2), m.group(3)
    yr_full = f"20{yr}" if len(yr) == 2 else yr
    try:
        return datetime.strptime(f"{day} {mon} {yr_full}", '%d %b %Y').date()
    except ValueError:
        return None


def extract_closures_for_day(ws):
    """
    Walks the closing tab to find the 'TOTAL CLOSED SINCE 10 AM' section header,
    then reads camp rows below it until a blank/section break.

    Returns: dict with totals + per-type breakdown.
    """
    rows = ws.get_all_values()
    in_total_section = False
    skipped_header   = False
    closures = []

    for r in rows:
        if not r:
            continue
        first = (r[0] or '').strip()
        # Section header detection
        if TOTAL_HEADER_RE.search(' '.join(r)):
            in_total_section = True
            skipped_header = False
            continue
        if not in_total_section:
            continue
        # Inside the section. Skip the column-header line (first non-empty after section start).
        if not skipped_header:
            if 'Bucket' in first or 'Campaign Name' in ' '.join(r):
                skipped_header = True
                continue
            # Skip subtitle/segmentation lines
            if 'Segmentation' in ' '.join(r):
                continue
            continue
        # Stop at the next big section or a fully blank row
        if first.startswith(('💀', '🚨', '⚠️', '📊', '📋', '✅')) and 'TOTAL CLOSED' not in ' '.join(r):
            break
        if all(not (c or '').strip() for c in r):
            # Blank row likely ends the section
            break
        # Data row layout: Bucket, Campaign Name, Type, Was Budget, Was Today ROAS, ...
        if len(r) < 4:
            continue
        camp_name = (r[1] or '').strip()
        camp_type = (r[2] or '').strip()
        was_budget = parse_money(r[3])
        if not camp_name or was_budget <= 0:
            continue
        closures.append({
            'name':   camp_name,
            'type':   camp_type or 'Unknown',
            'budget': was_budget,
        })
    return closures


def build_summary(by_day):
    rows = []
    rows.append(["📈  CUMULATIVE CLOSURES — SM portal  |  All days with a closing tab in this sheet", '', '', '', ''])

    rows.append(['Date', '# Camps Closed', 'Budget Freed (₹)', 'By Type', 'Top Categories (count)'])

    grand_n = grand_bud = 0
    type_grand = defaultdict(int)
    for d in sorted(by_day.keys(), reverse=True):
        camps = by_day[d]
        if not camps:
            continue
        n   = len(camps)
        bud = sum(c['budget'] for c in camps)
        tcount = defaultdict(int)
        tbud   = defaultdict(int)
        for c in camps:
            tcount[c['type']] += 1
            tbud[c['type']]   += c['budget']
            type_grand[c['type']] += c['budget']
        type_str = '  |  '.join(f"{t}: {tcount[t]} camps / ₹{tbud[t]:,}" for t in sorted(tcount, key=lambda x: -tbud[x]))
        rows.append([d.strftime('%d %b %Y'), n, bud, type_str, ''])
        grand_n += n; grand_bud += bud

    rows.append([''])
    rows.append(['GRAND TOTAL (all days)', grand_n, grand_bud, '', ''])
    if type_grand:
        rows.append([''])
        rows.append(['Type', 'Total Closures (₹)', '% of Grand Total', '', ''])
        total = sum(type_grand.values())
        for t, b in sorted(type_grand.items(), key=lambda kv: -kv[1]):
            share = (b / total * 100) if total else 0
            rows.append([t, b, f"{share:.1f}%", '', ''])
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.parse_args()

    creds = Credentials.from_service_account_file(SA_FILE, scopes=SCOPES)
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(SHEET_ID)

    by_day = {}
    for ws in sh.worksheets():
        d = parse_date_from_tab(ws.title)
        if not d:
            continue
        print(f"  reading {ws.title} ({d})…")
        try:
            closures = extract_closures_for_day(ws)
        except Exception as e:
            print(f"    ⚠️  {e}"); continue
        if closures:
            by_day[d] = closures
        else:
            print(f"    (no closures found in 'TOTAL CLOSED SINCE 10 AM' section)")

    rows = build_summary(by_day)

    tab_name = "📈 Cumulative Closures"
    existing = [w.title for w in sh.worksheets()]
    if tab_name in existing:
        sh.del_worksheet(sh.worksheet(tab_name))
    ws_out = sh.add_worksheet(title=tab_name, rows=max(200, len(rows) + 20), cols=8)
    sh.reorder_worksheets([ws_out] + [w for w in sh.worksheets() if w.title != tab_name])

    padded = [r + [''] * max(0, 5 - len(r)) for r in rows]
    ws_out.update(range_name='A1', values=padded, value_input_option='USER_ENTERED')

    # Header formatting
    sheet_id = ws_out.id
    def rgb(r,g,b): return {'red':r/255,'green':g/255,'blue':b/255}
    fmt_reqs = []
    for i, row in enumerate(padded, 1):
        v = (row[0] or '').strip()
        if v.startswith('📈'):
            fmt_reqs.append({'repeatCell': {
                'range': {'sheetId': sheet_id, 'startRowIndex': i-1, 'endRowIndex': i,
                          'startColumnIndex': 0, 'endColumnIndex': 5},
                'cell': {'userEnteredFormat': {
                    'backgroundColor': rgb(30, 60, 120),
                    'textFormat': {'bold': True, 'fontSize': 11, 'foregroundColor': rgb(255,255,255)}}},
                'fields': 'userEnteredFormat(backgroundColor,textFormat)'}})
        elif v in ('Date', 'Type'):
            fmt_reqs.append({'repeatCell': {
                'range': {'sheetId': sheet_id, 'startRowIndex': i-1, 'endRowIndex': i,
                          'startColumnIndex': 0, 'endColumnIndex': 5},
                'cell': {'userEnteredFormat': {
                    'backgroundColor': rgb(70, 70, 70),
                    'textFormat': {'bold': True, 'foregroundColor': rgb(255,255,255)}}},
                'fields': 'userEnteredFormat(backgroundColor,textFormat)'}})
        elif v.startswith('GRAND TOTAL'):
            fmt_reqs.append({'repeatCell': {
                'range': {'sheetId': sheet_id, 'startRowIndex': i-1, 'endRowIndex': i,
                          'startColumnIndex': 0, 'endColumnIndex': 5},
                'cell': {'userEnteredFormat': {
                    'backgroundColor': rgb(30, 30, 30),
                    'textFormat': {'bold': True, 'foregroundColor': rgb(255,255,180)}}},
                'fields': 'userEnteredFormat(backgroundColor,textFormat)'}})

    fmt_reqs.append({'updateDimensionProperties': {
        'range': {'sheetId': sheet_id, 'dimension': 'COLUMNS', 'startIndex': 3, 'endIndex': 4},
        'properties': {'pixelSize': 600}, 'fields': 'pixelSize'}})

    if fmt_reqs:
        sh.batch_update({'requests': fmt_reqs})

    n_days  = sum(1 for d in by_day if by_day[d])
    n_camps = sum(len(c) for c in by_day.values())
    print(f"\n✅ Wrote '{tab_name}' — {n_days} days, {n_camps} total closures")


if __name__ == '__main__':
    main()
