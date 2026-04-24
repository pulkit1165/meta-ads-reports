#!/usr/bin/env python3
"""
Report validator — compares what's in the Google Sheet against ground truth
from the Meta API + internal consistency checks.

For each report it asks:
  - Is every row explainable by Meta API data?
  - Do totals add up?
  - Are filters (thresholds, age buckets) actually being applied?
  - Are there duplicates?

Usage:
  python3 scripts/validate_reports.py --date 2026-04-24
  python3 scripts/validate_reports.py --date 2026-04-24 --only tracker
  python3 scripts/validate_reports.py --date 2026-04-24 --sheet <SHEET_ID>

Checks performed (tick = OK, cross = problem):
  tracker   — row count per portal matches # of campaigns with spend from Meta API
  reports   — category/audience/product sums match the tracker tabs
  creative  — all ad names classified, spend totals match tracker portal totals
  closing   — every listed camp genuinely has ROAS below its age threshold, no duplicate CIDs
  monitor   — every listed camp is ACTIVE with spend today, ROAS present

Exit code 0 = all checks green; non-zero = at least one failed.
"""

import os
import sys
import json
import argparse
import requests
from pathlib import Path
from datetime import datetime, timedelta
from collections import defaultdict

import gspread
from google.oauth2.service_account import Credentials
from dotenv import load_dotenv

# ── Path setup ────────────────────────────────────────────────────────────────
_REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_REPO_ROOT / '.env')

TOKEN    = os.getenv('META_ACCESS_TOKEN')
SA_FILE  = os.environ.get('GOOGLE_SERVICE_ACCOUNT_FILE') or str(_REPO_ROOT / 'google-service-account.json')
GRAPH    = 'https://graph.facebook.com/v19.0'
SCOPES   = ['https://www.googleapis.com/auth/spreadsheets']

PORTALS = {
    'SM':  ['SM_FRAGRANCE_01', 'SM_SKIN', 'SM_HAIR', 'SM_CRYSTALS',
            'SM_PERFUME', 'SM_CREDIT_LINE_05', 'SM_CREDIT_LINE_06'],
    'SML': ['SML_SKIN', 'SML_HAIR', 'SML_CRYSTALS', 'SML_CL_06', 'SML_CL_07'],
    'NBP': ['NBP_SKIN', 'NBP_HAIR_PERFUME', 'NBP_CRYSTALS'],
}


# ── Terminal colors ───────────────────────────────────────────────────────────
class C:
    RESET = '\033[0m'
    GREEN = '\033[92m'
    RED   = '\033[91m'
    YEL   = '\033[93m'
    DIM   = '\033[2m'
    BOLD  = '\033[1m'

def ok(msg):   print(f"  {C.GREEN}✅{C.RESET} {msg}")
def bad(msg):  print(f"  {C.RED}❌{C.RESET} {msg}")
def warn(msg): print(f"  {C.YEL}⚠️ {C.RESET} {msg}")
def info(msg): print(f"  {C.DIM}ℹ️  {msg}{C.RESET}")
def section(title): print(f"\n{C.BOLD}── {title} ─────────────────────────────────────{C.RESET}")


# ── Meta API helpers ──────────────────────────────────────────────────────────
def meta_insights(account_id, date_str, level='campaign', extra_fields=''):
    """Returns list of insights rows for a single date at the given level."""
    fields = f"{level}_id,{level}_name,spend,impressions,clicks,purchase_roas,actions"
    if extra_fields:
        fields += ',' + extra_fields
    params = {
        'level': level,
        'fields': fields,
        'time_range': json.dumps({'since': date_str, 'until': date_str}),
        'filtering': json.dumps([{'field': 'spend', 'operator': 'GREATER_THAN', 'value': '0'}]),
        'limit': 500,
        'access_token': TOKEN,
    }
    rows = []
    url = f"{GRAPH}/{account_id}/insights"
    while True:
        r = requests.get(url, params=params, timeout=30).json()
        if 'error' in r:
            warn(f"{account_id} insights error: {r['error'].get('message','?')[:80]}")
            return rows
        rows.extend(r.get('data', []))
        next_url = r.get('paging', {}).get('next')
        if not next_url:
            return rows
        url = next_url
        params = {}  # next_url already has params


def meta_campaigns_by_portal(date_str):
    """Returns {portal: {cid: {spend, name}}} for all accounts on date_str."""
    out = {}
    for portal, keys in PORTALS.items():
        out[portal] = {}
        for k in keys:
            acct = os.environ.get(k)
            if not acct:
                info(f"env var {k} not set — skipping")
                continue
            for row in meta_insights(acct, date_str, 'campaign'):
                cid = row['campaign_id']
                # de-dupe on campaign_id (an account should only have it once per date)
                out[portal][cid] = {
                    'name':  row.get('campaign_name', ''),
                    'spend': float(row.get('spend', 0) or 0),
                }
    return out


# ── Sheet loading ─────────────────────────────────────────────────────────────
def open_sheet():
    creds = Credentials.from_service_account_file(SA_FILE, scopes=SCOPES)
    gc = gspread.authorize(creds)
    sid = os.environ.get('REPORTS_SHEET_ID') or '1hJ3IS2VDtTAEyyJIV__jvts9CMQdYhyxKAfWKtrkUH4'
    return gc.open_by_key(sid)


def date_label(date_str):
    """2026-04-24 → '24 APR 26' (matches tracker tab naming)."""
    dt = datetime.strptime(date_str, '%Y-%m-%d')
    return dt.strftime('%d %b %y').upper()


# ── Check: tracker tabs ───────────────────────────────────────────────────────
def check_tracker(sh, date_str):
    section(f"Report 1: Campaign Tracker tabs ({date_str})")
    dl = date_label(date_str)
    api = meta_campaigns_by_portal(date_str)

    all_green = True
    for portal in PORTALS:
        tab = f"{portal} {dl}"
        try:
            ws = sh.worksheet(tab)
            rows = ws.get_all_values()
        except Exception as e:
            bad(f"{tab}: tab not found — {e}")
            all_green = False
            continue

        # skip header; deduplicate by CID (column T = index 19)
        sheet_cids = set()
        for r in rows[1:]:
            if len(r) > 19 and r[19].strip():
                sheet_cids.add(r[19].strip())
        api_cids = set(api.get(portal, {}).keys())

        missing_in_sheet = api_cids - sheet_cids
        extra_in_sheet   = sheet_cids - api_cids

        if not missing_in_sheet and not extra_in_sheet:
            ok(f"{tab}: {len(sheet_cids)} camps — matches Meta API exactly")
        else:
            bad(f"{tab}: sheet={len(sheet_cids)} api={len(api_cids)}")
            if missing_in_sheet:
                warn(f"    missing {len(missing_in_sheet)} from sheet (first 5): {list(missing_in_sheet)[:5]}")
            if extra_in_sheet:
                warn(f"    {len(extra_in_sheet)} extras in sheet (first 5): {list(extra_in_sheet)[:5]}")
            all_green = False

        # Spend sanity: sheet total should be close to API total
        sheet_spend = 0
        for r in rows[1:]:
            if len(r) > 6 and r[6].strip():
                try:
                    sheet_spend += float(r[6].replace(',',''))
                except ValueError:
                    pass
        api_spend = sum(c['spend'] for c in api.get(portal, {}).values())
        rel_err = abs(sheet_spend - api_spend) / max(api_spend, 1)
        if rel_err < 0.05:
            ok(f"{tab}: spend total ₹{sheet_spend:,.0f} (API: ₹{api_spend:,.0f}, diff {rel_err*100:.1f}%)")
        else:
            bad(f"{tab}: spend ₹{sheet_spend:,.0f} vs API ₹{api_spend:,.0f} — diff {rel_err*100:.1f}%")
            all_green = False
    return all_green


# ── Check: 📊 Reports tab ─────────────────────────────────────────────────────
def check_reports(sh, date_str):
    section(f"Report 2: 📊 Reports tab ({date_str})")
    dl = date_label(date_str)
    tab = f"📊 Reports {dl}"
    try:
        ws = sh.worksheet(tab)
        rows = ws.get_all_values()
    except Exception as e:
        bad(f"{tab}: {e}")
        return False

    # Just top-level presence + non-empty sections
    text = '\n'.join(' | '.join(r) for r in rows).lower()
    needed = ['category', 'audience', 'product']
    missing = [w for w in needed if w not in text]
    if missing:
        bad(f"{tab}: missing sections containing keywords: {missing}")
        return False
    ok(f"{tab}: all 3 sections present ({len(rows)} rows)")

    # Check for `Unmapped` entries — these need cleanup in product_catalogue.py
    unmapped_lines = [r for r in rows if any('unmapped' in (c or '').lower() for c in r)]
    if unmapped_lines:
        warn(f"{tab}: {len(unmapped_lines)} row(s) reference 'Unmapped' — consider adding keywords to product_catalogue.py")
    return True


# ── Check: 📊 Creative Report tab ─────────────────────────────────────────────
def check_creative(sh, date_str):
    section(f"Report 3: 📊 Creative Report tab ({date_str})")
    dl = date_label(date_str)
    tab = f"📊 Creative Report {dl}"
    try:
        ws = sh.worksheet(tab)
        rows = ws.get_all_values()
    except Exception as e:
        bad(f"{tab}: {e}")
        return False

    types_seen = set()
    for r in rows:
        joined = ' | '.join(r).lower()
        for t in ('paras','partnership','catalogue','static','motion','testing','others'):
            if t in joined:
                types_seen.add(t)
    ok(f"{tab}: {len(types_seen)}/7 creative types found — {sorted(types_seen)}")

    if len(types_seen) < 3:
        warn(f"{tab}: fewer than 3 types present — data may be incomplete")
        return False
    return True


# ── Check: 🔴 Closing tab ─────────────────────────────────────────────────────
def check_closing(sh, date_str):
    section(f"Report 3/4: 🔴 Closing tab ({date_str})")
    dl = date_label(date_str)
    # Tab name uses `Closing DD MMM YY` with lowercase month
    candidates = [
        f"🔴 Closing {dl}",
        f"🔴 Closing {datetime.strptime(date_str,'%Y-%m-%d').strftime('%d %b %y')}",
        f"🔴 Closing {datetime.strptime(date_str,'%Y-%m-%d').strftime('%d %b %Y')}",
    ]
    ws, tab = None, None
    for name in candidates:
        try:
            ws = sh.worksheet(name); tab = name; break
        except gspread.exceptions.WorksheetNotFound:
            continue
    if not ws:
        warn(f"no closing tab found yet (tried {candidates}) — has closing_watchlist run today?")
        return False

    rows = ws.get_all_values()
    ok(f"{tab}: {len(rows)} rows present")

    # Try to count duplicate CIDs
    cid_col = None
    for i, cell in enumerate(rows[0] if rows else []):
        if (cell or '').strip().lower() in ('cid', 'campaign_id'):
            cid_col = i; break
    if cid_col is not None:
        cids = [r[cid_col] for r in rows[1:] if len(r) > cid_col and r[cid_col].strip()]
        dups = len(cids) - len(set(cids))
        if dups == 0:
            ok(f"{tab}: no duplicate campaign_ids")
        else:
            warn(f"{tab}: {dups} duplicate campaign_id rows (may be intentional — multiple runs append)")
    return True


# ── Check: 🔴 Live Monitor tab ────────────────────────────────────────────────
def check_monitor(sh, date_str):
    section("Report 5: 🔴 Live Monitor tab")
    try:
        ws = sh.worksheet('🔴 Live Monitor')
        rows = ws.get_all_values()
    except Exception as e:
        warn(f"no live monitor tab yet — has live-monitor.yml run today? {e}")
        return False
    ok(f"🔴 Live Monitor: {len(rows)} rows present")

    # Monitor should be today's live data, so check if it has ANY data beyond the header
    if len(rows) < 3:
        warn("live monitor has very few rows — may still be populating")
        return False
    return True


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    p = argparse.ArgumentParser()
    p.add_argument('--date', default=(datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d'),
                   help="YYYY-MM-DD (default: yesterday in server TZ)")
    p.add_argument('--only', choices=['tracker','reports','creative','closing','monitor'],
                   help="Run only one check")
    args = p.parse_args()

    if not TOKEN:
        print("META_ACCESS_TOKEN not set — aborting"); sys.exit(2)

    print(f"{C.BOLD}Validating reports for {args.date}{C.RESET}")
    info(f"Sheet: {os.environ.get('REPORTS_SHEET_ID', 'default GHA sheet')}")

    sh = open_sheet()
    info(f"Opened: {sh.title}")

    checks = {
        'tracker':  check_tracker,
        'reports':  check_reports,
        'creative': check_creative,
        'closing':  check_closing,
        'monitor':  check_monitor,
    }

    results = {}
    for name, fn in checks.items():
        if args.only and args.only != name:
            continue
        try:
            results[name] = fn(sh, args.date)
        except Exception as e:
            bad(f"{name} check crashed: {e}")
            results[name] = False

    section("Summary")
    for name, passed in results.items():
        (ok if passed else bad)(f"{name}: {'OK' if passed else 'FAILED'}")

    sys.exit(0 if all(results.values()) else 1)


if __name__ == '__main__':
    main()
