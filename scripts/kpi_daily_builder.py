#!/usr/bin/env python3
"""
KPI Daily Builder — auto-populates the dashboard's per-portal Meta Ads KPIs.

The NTN dashboard reads a 'KPI Daily' tab with one row per (date, portal)
covering 21 metrics: Spend, Impressions, Reach, Frequency, CPM, CTR, CPR/1L
Reach, Outbound Clicks, LPV, ATC, ATC Rate, LP CVR, Purchases, CPC, CPP,
Thumbstop %, Hold Rate %, Revenue, ROAS. The original NTN source sheet has
this tab populated manually by the operator → frequently lags by days.

This script aggregates the same metrics directly from Meta insights at
`level=account`, summed across each portal's accounts, and writes them to
the GHA-owned sheet's 'KPI Daily' tab. The dashboard then prefers GHA's
data over NTN's, falling back to NTN for any older dates GHA doesn't have.

Usage:
  python3 scripts/kpi_daily_builder.py                  # last 7 days
  python3 scripts/kpi_daily_builder.py --days 14
  python3 scripts/kpi_daily_builder.py --date 2026-04-26
  python3 scripts/kpi_daily_builder.py --since 2026-04-01 --until 2026-04-26
"""
import os, sys, json, time, argparse, requests
from pathlib import Path
from datetime import datetime, timedelta
from collections import defaultdict
from zoneinfo import ZoneInfo

import gspread
from google.oauth2.service_account import Credentials
from dotenv import load_dotenv

_REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_REPO_ROOT / '.env')

TOKEN    = os.getenv('META_ACCESS_TOKEN')
SA_FILE  = os.environ.get('GOOGLE_SERVICE_ACCOUNT_FILE') or str(_REPO_ROOT / 'google-service-account.json')
SHEET_ID = os.environ.get('REPORTS_SHEET_ID') or '1hJ3IS2VDtTAEyyJIV__jvts9CMQdYhyxKAfWKtrkUH4'
SCOPES   = ['https://www.googleapis.com/auth/spreadsheets']
GRAPH    = 'https://graph.facebook.com/v19.0'
IST      = ZoneInfo('Asia/Kolkata')

PORTALS = {
    'SM':  ['SM_FRAGRANCE_01', 'SM_SKIN', 'SM_HAIR', 'SM_CRYSTALS',
            'SM_PERFUME', 'SM_CREDIT_LINE_05', 'SM_CREDIT_LINE_06'],
    'SML': ['SML_SKIN', 'SML_HAIR', 'SML_CRYSTALS', 'SML_CL_06', 'SML_CL_07'],
    'NBP': ['NBP_SKIN', 'NBP_HAIR_PERFUME', 'NBP_CRYSTALS'],
}

HEADERS = [
    'Date', 'Portal', 'Spend (₹)', 'Impressions', 'Reach', 'Frequency',
    'CPM (₹)', 'CTR (%)', 'CPR/1L Reach (₹)', 'Outbound Clicks', 'LPV',
    'ATC', 'ATC Rate (%)', 'LP CVR (%)', 'Purchases (Meta)', 'CPC (₹)',
    'CPP (₹)', 'Thumbstop (%)', 'Hold Rate (%)', 'Revenue (Meta ₹)', 'ROAS',
]

# Meta insights fields we need
INSIGHTS_FIELDS = [
    'spend', 'impressions', 'reach', 'frequency', 'cpm', 'cpc', 'cpp', 'ctr',
    'inline_link_clicks',
    'actions', 'action_values',
    'video_p25_watched_actions', 'video_p75_watched_actions',
    'outbound_clicks',
]


def safe_float(x, default=0.0):
    try: return float(x or 0)
    except (TypeError, ValueError): return default

def safe_int(x, default=0):
    try: return int(float(x or 0))
    except (TypeError, ValueError): return default


def fetch_account_kpis(account_id, date_str, retries=3):
    """One day's account-level insights for a single ad account.
    Returns the raw insights dict (single row) or {} if no spend that day."""
    if not account_id:
        return {}
    params = {
        'level': 'account',
        'fields': ','.join(INSIGHTS_FIELDS),
        'time_range': json.dumps({'since': date_str, 'until': date_str}),
        'access_token': TOKEN,
    }
    url = f'{GRAPH}/{account_id}/insights'
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, timeout=30).json()
        except Exception as e:
            if attempt + 1 == retries:
                print(f"    ⚠️  {account_id} {date_str} request error: {e}", file=sys.stderr)
                return {}
            time.sleep(2 ** attempt)
            continue
        if 'error' in r:
            err = r['error']
            if err.get('code') == 17:  # rate limit
                time.sleep(60); continue
            print(f"    ⚠️  {account_id} {date_str}: {err.get('message','?')[:80]}", file=sys.stderr)
            return {}
        data = r.get('data') or []
        return data[0] if data else {}
    return {}


def _action_sum(actions_list, types):
    """Sum the 'value' of items in actions list whose action_type is in `types`."""
    if not actions_list:
        return 0.0
    total = 0.0
    for a in actions_list:
        if a.get('action_type') in types:
            total += safe_float(a.get('value', 0))
    return total


def aggregate_portal(date_str, portal, account_keys):
    """Aggregate insights across all accounts in a portal for one date."""
    agg = {
        'spend': 0.0, 'impressions': 0, 'reach': 0,
        'inline_link_clicks': 0,
        'outbound_clicks': 0,
        'video_p25': 0, 'video_p75': 0,
        'purchases': 0, 'revenue': 0.0,
        'lpv': 0, 'atc': 0,
    }
    for key in account_keys:
        acct_id = os.environ.get(key)
        if not acct_id:
            continue
        d = fetch_account_kpis(acct_id, date_str)
        if not d:
            continue
        agg['spend']        += safe_float(d.get('spend'))
        agg['impressions']  += safe_int(d.get('impressions'))
        agg['reach']        += safe_int(d.get('reach'))
        agg['inline_link_clicks'] += safe_int(d.get('inline_link_clicks'))

        # outbound_clicks is a list of {action_type, value}
        agg['outbound_clicks'] += int(_action_sum(d.get('outbound_clicks'), {'outbound_click'}))
        agg['video_p25']       += int(_action_sum(d.get('video_p25_watched_actions'), {'video_view'}))
        agg['video_p75']       += int(_action_sum(d.get('video_p75_watched_actions'), {'video_view'}))

        # actions list: purchase, lpv, atc — Meta uses both 'omni_*' and bare names
        actions = d.get('actions') or []
        agg['purchases'] += int(_action_sum(actions, {'omni_purchase', 'purchase'}))
        agg['lpv']       += int(_action_sum(actions, {'landing_page_view'}))
        agg['atc']       += int(_action_sum(actions, {'omni_add_to_cart', 'add_to_cart'}))

        action_values = d.get('action_values') or []
        agg['revenue'] += _action_sum(action_values, {'omni_purchase', 'purchase'})

    return _derive_kpi_row(date_str, portal, agg)


def _derive_kpi_row(date_str, portal, agg):
    spend       = agg['spend']
    impressions = agg['impressions']
    reach       = agg['reach']
    clicks      = agg['inline_link_clicks']
    purchases   = agg['purchases']
    revenue     = agg['revenue']
    lpv         = agg['lpv']
    atc         = agg['atc']

    def safe_div(n, d, dec=2):
        return round(n / d, dec) if d else 0.0

    return {
        'Date': date_str,
        'Portal': portal,
        'Spend (₹)':       round(spend, 2),
        'Impressions':     impressions,
        'Reach':           reach,
        'Frequency':       safe_div(impressions, reach),
        'CPM (₹)':         safe_div(spend, impressions / 1000) if impressions else 0,
        'CTR (%)':         safe_div(clicks * 100, impressions),
        'CPR/1L Reach (₹)': safe_div(spend, reach / 100000) if reach else 0,
        'Outbound Clicks': agg['outbound_clicks'],
        'LPV':             lpv,
        'ATC':             atc,
        'ATC Rate (%)':    safe_div(atc * 100, lpv),
        'LP CVR (%)':      safe_div(purchases * 100, lpv),
        'Purchases (Meta)': purchases,
        'CPC (₹)':         safe_div(spend, clicks),
        'CPP (₹)':         safe_div(spend, purchases),
        'Thumbstop (%)':   safe_div(agg['video_p25'] * 100, impressions),
        'Hold Rate (%)':   safe_div(agg['video_p75'] * 100, impressions),
        'Revenue (Meta ₹)': round(revenue, 2),
        'ROAS':            safe_div(revenue, spend),
    }


def aggregate_total(portal_rows):
    """Build a 'Total' row by summing absolute values + recomputing derived ratios."""
    agg = {
        'spend': 0.0, 'impressions': 0, 'reach': 0,
        'inline_link_clicks': 0, 'outbound_clicks': 0,
        'video_p25': 0, 'video_p75': 0,
        'purchases': 0, 'revenue': 0.0,
        'lpv': 0, 'atc': 0,
    }
    if not portal_rows:
        return None
    date_str = portal_rows[0]['Date']
    # Reverse-engineer the absolute totals from the row dicts. Easier: track
    # them on the side. Simplest: re-derive ratios from the absolute fields.
    for r in portal_rows:
        agg['spend']       += safe_float(r['Spend (₹)'])
        agg['impressions'] += safe_int(r['Impressions'])
        agg['reach']       += safe_int(r['Reach'])
        # We don't have raw clicks back; reconstruct from CPC/spend
        clicks_r = round(safe_float(r['Spend (₹)']) / safe_float(r['CPC (₹)'])) if safe_float(r['CPC (₹)']) else 0
        agg['inline_link_clicks'] += clicks_r
        agg['outbound_clicks'] += safe_int(r['Outbound Clicks'])
        # Video metrics — we have % so reconstruct
        agg['video_p25'] += int(safe_float(r['Thumbstop (%)']) / 100 * safe_int(r['Impressions']))
        agg['video_p75'] += int(safe_float(r['Hold Rate (%)']) / 100 * safe_int(r['Impressions']))
        agg['purchases']   += safe_int(r['Purchases (Meta)'])
        agg['revenue']     += safe_float(r['Revenue (Meta ₹)'])
        agg['lpv']         += safe_int(r['LPV'])
        agg['atc']         += safe_int(r['ATC'])

    return _derive_kpi_row(date_str, 'Total', agg)


def write_to_sheet(rows):
    """Upsert rows into the GHA sheet's 'KPI Daily' tab.
    Replaces any existing rows with same (Date, Portal); keeps the rest."""
    if not rows:
        print("No rows to write.")
        return
    creds = Credentials.from_service_account_file(SA_FILE, scopes=SCOPES)
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(SHEET_ID)

    titles = [w.title for w in sh.worksheets()]
    if 'KPI Daily' not in titles:
        ws = sh.add_worksheet(title='KPI Daily', rows=500, cols=len(HEADERS))
        ws.update(range_name='A1', values=[HEADERS], value_input_option='USER_ENTERED')
        existing = []
    else:
        ws = sh.worksheet('KPI Daily')
        existing = ws.get_all_values()
        if not existing:
            ws.update(range_name='A1', values=[HEADERS], value_input_option='USER_ENTERED')
            existing = [HEADERS]

    # Index existing rows by (date, portal)
    header = existing[0] if existing else HEADERS
    by_key = {}
    for r in existing[1:]:
        if len(r) >= 2 and r[0] and r[1]:
            by_key[(r[0], r[1])] = r

    # Update / insert from incoming rows
    for row in rows:
        key = (row['Date'], row['Portal'])
        flat = [row.get(h, '') for h in HEADERS]
        by_key[key] = flat

    # Re-sort by date desc then portal order (Total, SM, SML, NBP)
    portal_order = {'Total': 0, 'SM': 1, 'SML': 2, 'NBP': 3}
    sorted_rows = sorted(
        by_key.items(),
        key=lambda kv: (-datetime.strptime(kv[0][0], '%Y-%m-%d').toordinal()
                        if _is_date(kv[0][0]) else 0,
                        portal_order.get(kv[0][1], 99)),
    )

    out = [HEADERS] + [v for _, v in sorted_rows]
    ws.clear()
    ws.update(range_name='A1', values=out, value_input_option='USER_ENTERED')
    print(f"  ✅ Wrote {len(out) - 1} rows to 'KPI Daily' (added/updated {len(rows)})")


def _is_date(s):
    try:
        datetime.strptime(s, '%Y-%m-%d'); return True
    except (ValueError, TypeError):
        return False


def date_range(args):
    if args.date:
        d = datetime.strptime(args.date, '%Y-%m-%d')
        return [d]
    if args.since and args.until:
        d0 = datetime.strptime(args.since, '%Y-%m-%d')
        d1 = datetime.strptime(args.until, '%Y-%m-%d')
        return [d0 + timedelta(days=i) for i in range((d1 - d0).days + 1)]
    # Default: last N days through yesterday IST
    end = datetime.now(IST).date() - timedelta(days=1)
    return [datetime.combine(end - timedelta(days=i), datetime.min.time())
            for i in range(args.days - 1, -1, -1)]


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--date', help='Single date YYYY-MM-DD')
    p.add_argument('--since', help='Start date YYYY-MM-DD')
    p.add_argument('--until', help='End date YYYY-MM-DD')
    p.add_argument('--days', type=int, default=7, help='Last N days through yesterday IST (default 7)')
    args = p.parse_args()

    if not TOKEN:
        print("META_ACCESS_TOKEN not set"); sys.exit(2)

    dates = date_range(args)
    print(f"\n📊 KPI Daily Builder — {len(dates)} date(s): {dates[0].strftime('%Y-%m-%d')} → {dates[-1].strftime('%Y-%m-%d')}\n")

    all_rows = []
    for d in dates:
        ds = d.strftime('%Y-%m-%d')
        print(f"→ {ds}")
        portal_rows = []
        for portal, keys in PORTALS.items():
            row = aggregate_portal(ds, portal, keys)
            portal_rows.append(row)
            print(f"   {portal:<4} spend ₹{int(row['Spend (₹)']):>10,}  imp {row['Impressions']:>11,}  "
                  f"purchases {row['Purchases (Meta)']:>5}  ROAS {row['ROAS']:.2f}x")
        total_row = aggregate_total(portal_rows)
        if total_row:
            print(f"   Total spend ₹{int(total_row['Spend (₹)']):>10,}  ROAS {total_row['ROAS']:.2f}x")
            all_rows.append(total_row)
        all_rows.extend(portal_rows)

    print()
    write_to_sheet(all_rows)


if __name__ == '__main__':
    main()
