#!/usr/bin/env python3
"""Push today's creative success rate (pivot + ad-level detail) to a Google Sheet.

Reads the most recent creative_success_rate_<date>.csv from out/ and writes:
  - Top block: pivot (bucket, sum spend, avg ROAS, #ads)
  - Below: ad-level detail rows grouped by bucket with ad_id + preview/manager links

Usage:
  python scripts/push_success_rate_to_sheet.py [YYYY-MM-DD] [--min-spend=N]
                                                [--sheet-id=...] [--tab=...]
"""
import os, sys, csv
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
import gspread
from google.oauth2.service_account import Credentials
from dotenv import load_dotenv

_REPO = Path(__file__).resolve().parent.parent
load_dotenv(_REPO / '.env')

IST = ZoneInfo('Asia/Kolkata')
SA_FILE = os.environ.get('GOOGLE_SERVICE_ACCOUNT_FILE') or str(_REPO / 'google-service-account.json')
SCOPES = ['https://www.googleapis.com/auth/spreadsheets']
DEFAULT_SHEET = '1L4wOeOSNB2K_eutfLkRW16RcoHYUIcDMyZ6OLJ3CGtg'


def parse_args():
    args = list(sys.argv[1:])
    date_str = datetime.now(IST).strftime('%Y-%m-%d')
    min_spend = 500.0
    sheet_id = DEFAULT_SHEET
    tab = None
    for a in args:
        if a.startswith('--min-spend='):
            min_spend = float(a.split('=', 1)[1])
        elif a.startswith('--sheet-id='):
            sheet_id = a.split('=', 1)[1]
        elif a.startswith('--tab='):
            tab = a.split('=', 1)[1]
        else:
            date_str = a
    return date_str, min_spend, sheet_id, tab


def load_csv(date_str, min_spend):
    suffix = f"_minspend{int(min_spend)}" if min_spend != 500 else ''
    csv_path = _REPO / 'out' / f"creative_success_rate_{date_str}{suffix}.csv"
    if not csv_path.exists():
        sys.exit(f"CSV not found: {csv_path}\nRun creative_success_rate.py first.")
    rows = list(csv.DictReader(open(csv_path, encoding='utf-8')))
    print(f"Loaded {len(rows)} rows from {csv_path.name}")
    return rows


def build_pivot(rows):
    buckets = ['Roas >1.5', 'Roas >2', 'Roas >2.5', 'Roas >3']
    pivot = []
    total_spend = total_roas = 0.0
    total_n = 0
    for b in buckets:
        in_b = [r for r in rows if r['bucket'] == b]
        spend = sum(float(r['spend_inr']) for r in in_b)
        avg = (sum(float(r['roas']) for r in in_b) / len(in_b)) if in_b else 0.0
        pivot.append([b, round(spend, 2), round(avg, 6), len(in_b)])
        total_spend += spend
        total_roas += sum(float(r['roas']) for r in in_b)
        total_n += len(in_b)
    grand = (total_roas / total_n) if total_n else 0.0
    pivot.append(['Grand Total', round(total_spend, 2), round(grand, 6), total_n])
    return pivot


def main():
    date_str, min_spend, sheet_id, tab = parse_args()
    if not tab:
        tab = f"Success {date_str}" + (f" min{int(min_spend)}" if min_spend != 500 else '')

    rows = load_csv(date_str, min_spend)
    pivot = build_pivot(rows)

    creds = Credentials.from_service_account_file(SA_FILE, scopes=SCOPES)
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(sheet_id)

    # Clean + recreate tab
    try:
        existing = sh.worksheet(tab)
        sh.del_worksheet(existing)
    except gspread.WorksheetNotFound:
        pass
    ws = sh.add_worksheet(title=tab, rows=max(200, len(rows) + 30), cols=10)

    # Layout
    block = []
    block.append([f"Creative Success Rate — {date_str} (IST)   min spend: {min_spend}"])
    block.append([f"Source: all 3 portals (NBP + SM + SML), ad-level Meta insights"])
    block.append([])
    block.append(['Row Labels', 'Sum of Amount spent (INR)', 'Average of roas 1D', '# Ads'])
    for p in pivot:
        block.append(p)
    block.append([])
    block.append(['Ad-level detail (sorted by ROAS desc) — only ads with ROAS >= 1.5'])
    block.append(['bucket', 'roas', 'spend_inr', 'ad_id', 'ad_name',
                  'portal', 'account', 'preview_link', 'ads_manager_url'])

    # Sort ad rows by roas desc within bucket order (>=3 first, then 2.5, 2, 1.5)
    bucket_order = {'Roas >3': 0, 'Roas >2.5': 1, 'Roas >2': 2, 'Roas >1.5': 3}
    rows_sorted = sorted(
        rows,
        key=lambda r: (bucket_order.get(r['bucket'], 9), -float(r['roas'])),
    )
    for r in rows_sorted:
        block.append([
            r['bucket'],
            float(r['roas']),
            float(r['spend_inr']),
            r['ad_id'],
            r['ad_name'],
            r['portal'],
            r['account'],
            r['preview_link'],
            r['ads_manager_url'],
        ])

    ws.update(values=block, range_name='A1', value_input_option='USER_ENTERED')

    # Light formatting on the pivot header row
    ws.format('A4:D4', {'textFormat': {'bold': True}})
    ws.format('A1:A1', {'textFormat': {'bold': True, 'fontSize': 12}})
    pivot_total_row = 4 + len(pivot)
    ws.format(f'A{pivot_total_row}:D{pivot_total_row}', {'textFormat': {'bold': True}})

    # Ad-level detail rows start at row 13 (after pivot block + section header + col headers)
    detail_start_row = pivot_total_row + 4  # blank, section title, col headers, first data row
    # Bold the detail column header row
    ws.format(f'A{detail_start_row - 1}:I{detail_start_row - 1}',
              {'textFormat': {'bold': True}})

    # Winner highlighting:
    #   GREEN  = roas >= 2 AND spend >= 500  (trustworthy winner)
    #   YELLOW = roas >= 2 AND spend < 500   (promising, small sample — worth scaling)
    GREEN  = {'red': 0.72, 'green': 0.88, 'blue': 0.72}  # light green
    YELLOW = {'red': 1.00, 'green': 0.95, 'blue': 0.70}  # light yellow

    green_rows, yellow_rows = [], []
    for i, r in enumerate(rows_sorted):
        roas = float(r['roas'])
        spend = float(r['spend_inr'])
        sheet_row = detail_start_row + i
        if roas >= 2.0 and spend >= 500:
            green_rows.append(sheet_row)
        elif roas >= 2.0 and spend < 500:
            yellow_rows.append(sheet_row)

    def to_ranges(row_list):
        """Collapse a sorted list of row numbers into A1 ranges over cols A:I."""
        if not row_list:
            return []
        out, start, prev = [], row_list[0], row_list[0]
        for r in row_list[1:]:
            if r == prev + 1:
                prev = r
            else:
                out.append(f'A{start}:I{prev}')
                start = prev = r
        out.append(f'A{start}:I{prev}')
        return out

    batch_formats = []
    for rng in to_ranges(green_rows):
        batch_formats.append({'range': rng, 'format': {'backgroundColor': GREEN}})
    for rng in to_ranges(yellow_rows):
        batch_formats.append({'range': rng, 'format': {'backgroundColor': YELLOW}})

    if batch_formats:
        ws.batch_format(batch_formats)
        print(f"Highlighted {len(green_rows)} winners (green), "
              f"{len(yellow_rows)} promising (yellow)")

    # Legend at the bottom
    legend_row = detail_start_row + len(rows_sorted) + 2
    ws.update(values=[
        ['Legend:'],
        ['Winner (ROAS ≥ 2 AND spend ≥ ₹500)'],
        ['Promising — small sample (ROAS ≥ 2 AND spend < ₹500)'],
    ], range_name=f'A{legend_row}', value_input_option='USER_ENTERED')
    ws.format(f'A{legend_row}', {'textFormat': {'bold': True}})
    ws.format(f'A{legend_row + 1}', {'backgroundColor': GREEN})
    ws.format(f'A{legend_row + 2}', {'backgroundColor': YELLOW})

    url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit#gid={ws.id}"
    print(f"Wrote tab '{tab}' with {len(pivot)} pivot rows + {len(rows_sorted)} ad rows")
    print(f"URL: {url}")


if __name__ == '__main__':
    main()
