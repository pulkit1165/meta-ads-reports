#!/usr/bin/env python3
"""
Active Budget by Product — per-portal report writer.

Pulls every currently-ACTIVE campaign from SM (excl SM_CREDIT_LINE_06), SML, NBP,
classifies each by product (with inline fixes for wanda/astro misclassification),
fetches the actual audience name from ad-set targeting, and writes 3 separate
per-portal tabs to the "Daily Camp Pushed" sheet:

    📊 SM — Active Budget by Product DD MMM YY
    📊 SML — Active Budget by Product DD MMM YY
    📊 NBP — Active Budget by Product DD MMM YY

Each tab has:
  - PRODUCT ROLLUP — Product / Category / #Camps / Daily Budget / Share%
  - PER-CAMP DETAIL — every campaign with Camp ID + Account + Segment (real
    custom-audience name from Meta API) + Budget

Designed for the cron schedule in .github/workflows/active-budget-by-product.yml
(10:00 IST + 21:00 IST). Each run overwrites the day's tab; a new day creates
new tabs (so the workbook builds a daily history).

Inline-fixes (NOT yet ported back to scripts/product_catalogue.py):
  1. Campaigns whose name contains 'wanda' AND an astro/crystal-product keyword
     should classify by the more specific product, not as 'Jewellery'.
  2. The catalogue's `astro.*re` rule over-matches `astro_destiny_report`
     (because 'report' contains 're') — handle astro_destiny / astro_bot
     explicitly first.
"""
import os
import json
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
import gspread
from google.oauth2.service_account import Credentials

sys.path.insert(0, str(Path(__file__).parent))
from product_catalogue import derive_product_and_category as _orig_classify, _norm

GRAPH = "https://graph.facebook.com/v19.0"

# Hard-coded sheet — this is the operator's "Daily Camp Pushed" workbook. The
# service account `antriksh-bot@antriksh-meta-reports.iam.gserviceaccount.com`
# already has Editor access on it.
SHEET_ID = "1eW2_qPdsKJ8zAV5-hsXA5HtfVH9NwDhQLyHYGKz5hXk"

PORTAL_ACCOUNTS = {
    'SM': [
        ('SM_FRAGRANCE_01',   'SM_FRAGRANCE_01'),
        ('SM_SKIN',           'SM_SKIN'),
        ('SM_HAIR',           'SM_HAIR'),
        ('SM_CRYSTALS',       'SM_CRYSTALS'),
        ('SM_PERFUME',        'SM_PERFUME'),
        ('SM_CREDIT_LINE_05', 'SM_CREDIT_LINE_05'),
        # SM_CREDIT_LINE_06 is intentionally excluded from this report.
    ],
    'SML': [
        ('SML_SKIN',     'SML_SKIN'),
        ('SML_HAIR',     'SML_HAIR'),
        ('SML_CRYSTALS', 'SML_CRYSTALS'),
        ('SML_CL_06',    'SML_CL_06'),
        ('SML_CL_07',    'SML_CL_07'),
    ],
    'NBP': [
        ('NBP_SKIN',         'NBP_SKIN'),
        ('NBP_HAIR_PERFUME', 'NBP_HAIR_PERFUME'),
        ('NBP_CRYSTALS',     'NBP_CRYSTALS'),
    ],
}

PORTAL_COLOR = {
    'SM':  {'red': 0.86, 'green': 0.92, 'blue': 1.00},  # blue
    'SML': {'red': 0.92, 'green': 0.97, 'blue': 0.86},  # green
    'NBP': {'red': 1.00, 'green': 0.95, 'blue': 0.86},  # orange
}


# ──────────────────────────────────────────────────────────────────────────────
# Meta API helpers
# ──────────────────────────────────────────────────────────────────────────────

def _token():
    t = os.environ.get('META_ACCESS_TOKEN')
    if not t:
        sys.exit("META_ACCESS_TOKEN env var is required")
    return t


def paginate(endpoint, params, retries=4):
    """GET with pagination + rate-limit retry. Returns flat list of records."""
    out = []
    params = dict(params)
    params['access_token'] = _token()
    for k in range(retries):
        try:
            r = requests.get(f"{GRAPH}/{endpoint}", params=params, timeout=60)
            d = r.json()
        except requests.exceptions.RequestException as e:
            print(f"  net err {endpoint}: {e}, retry in {5*(k+1)}s", file=sys.stderr)
            time.sleep(5 * (k + 1))
            continue
        if 'error' in d:
            msg = d['error'].get('message', '')
            if 'request limit' in msg.lower() or d['error'].get('code') in (17, 4, 80004):
                print(f"  rate-limit {endpoint}, sleep {30*(k+1)}s", file=sys.stderr)
                time.sleep(30 * (k + 1))
                continue
            print(f"  ERR {endpoint}: {msg}", file=sys.stderr)
            return out
        out.extend(d.get('data', []))
        nxt = d.get('paging', {}).get('next')
        while nxt:
            for k2 in range(retries):
                try:
                    r = requests.get(nxt, timeout=60); d = r.json()
                except requests.exceptions.RequestException:
                    time.sleep(5 * (k2 + 1)); continue
                if 'error' in d and 'request limit' in d['error'].get('message', '').lower():
                    time.sleep(30 * (k2 + 1)); continue
                break
            out.extend(d.get('data', []))
            nxt = d.get('paging', {}).get('next')
        return out
    return out


def get_with_retry(url_path, params, retries=4):
    """GET single response (no pagination). Returns dict."""
    params = dict(params)
    params['access_token'] = _token()
    for k in range(retries):
        try:
            r = requests.get(f"{GRAPH}/{url_path}", params=params, timeout=60)
            d = r.json()
        except requests.exceptions.RequestException:
            time.sleep(5 * (k + 1))
            continue
        if 'error' in d:
            msg = d['error'].get('message', '')
            if 'request limit' in msg.lower() or d['error'].get('code') in (17, 4, 80004):
                time.sleep(25 * (k + 1)); continue
            return d
        return d
    return {}


# ──────────────────────────────────────────────────────────────────────────────
# Classification — corrected for wanda/astro bug
# ──────────────────────────────────────────────────────────────────────────────

def classify_corrected(name):
    """
    Wraps derive_product_and_category() with two precedence fixes:
      1. astro_destiny / astro_bot patterns win over the generic 'astro.*re' rule
      2. wanda + astro/crystal terms strip 'wanda' before classifying so the
         specific product keyword wins over the generic 'wanda → Jewellery' rule.
    """
    n = _norm(name)

    # Astro-specific overrides
    if 'astro' in n or 'destiny' in n:
        if re.search(r'astro.*destiny|destiny.*report', n):
            return 'Astro Destiny Report', 'Mix'
        if re.search(r'astro.*bot|astro_bot|chatbot|aibot|ai_bot', n):
            return 'Astro Bot', 'Mix'

    # wanda + astro/crystal-specific keyword: strip wanda and re-classify so
    # the catalogue's specific rules can win without the wanda→Jewellery shortcut.
    if 'wanda' in n and re.search(
        r'astro|destiny|crystal|pyrite|selenite|peacock|horse_clock|'
        r'hourglass|geode|coaster|sutra|owl|nazar|miniature|sleek_crystal',
        n,
    ):
        stripped = re.sub(r'(^|_)wanda_?', '_', name, flags=re.IGNORECASE)
        return _orig_classify(stripped)

    return _orig_classify(name)


# ──────────────────────────────────────────────────────────────────────────────
# Audience name from ad-set targeting
# ──────────────────────────────────────────────────────────────────────────────

def fetch_audience_map(cids):
    """For each campaign id, fetch its ad-set targeting and return a dict:
        cid -> {'inc': [audience names], 'exc': [...], 'has_int': bool}
    """
    audience = {}
    for i in range(0, len(cids), 25):
        batch = cids[i:i+25]
        d = get_with_retry("", {
            'ids': ",".join(batch),
            'fields': (
                'id,adsets.limit(50){effective_status,'
                'targeting{custom_audiences,excluded_custom_audiences,'
                'interests,flexible_spec}}'
            ),
        })
        for cid, obj in (d or {}).items():
            if not isinstance(obj, dict) or 'error' in obj:
                audience[cid] = {'inc': [], 'exc': [], 'has_int': False}
                continue
            ads = (obj.get('adsets') or {}).get('data', [])
            included, excluded = [], []
            has_interests = False
            for a in ads:
                t = a.get('targeting') or {}
                for ca in (t.get('custom_audiences') or []):
                    included.append(ca.get('name', '(unnamed)'))
                for ca in (t.get('excluded_custom_audiences') or []):
                    excluded.append(ca.get('name', '(unnamed)'))
                if t.get('interests') or any(
                        fs.get('interests') for fs in (t.get('flexible_spec') or [])):
                    has_interests = True
            audience[cid] = {
                'inc': list(dict.fromkeys(included)),
                'exc': list(dict.fromkeys(excluded)),
                'has_int': has_interests,
            }
        time.sleep(0.7)
    return audience


def segment_label(info):
    if info.get('inc'):
        return info['inc'][0]
    if info.get('exc'):
        return f"Excl: {info['exc'][0]}"
    if info.get('has_int'):
        return 'Interests/Loose'
    return 'Loose'


# ──────────────────────────────────────────────────────────────────────────────
# Data collection
# ──────────────────────────────────────────────────────────────────────────────

def collect_camps():
    """Returns: portal -> product -> {'cat': str, 'camps': [{cid,name,account,budget,segment}]}."""
    by_portal = defaultdict(lambda: defaultdict(lambda: {'cat': '?', 'camps': []}))
    all_cids = []
    raw_records = defaultdict(list)  # (portal, product, category) -> [camp]

    for portal, accts in PORTAL_ACCOUNTS.items():
        for label, env_var in accts:
            aid = os.environ.get(env_var)
            if not aid:
                print(f"  ⚠️  {env_var} not set, skipping", file=sys.stderr)
                continue
            camps = paginate(f"{aid}/campaigns", {
                'fields': ('id,name,effective_status,daily_budget,lifetime_budget,'
                           'adsets.limit(50){effective_status,daily_budget,lifetime_budget}'),
                'effective_status': '["ACTIVE"]',
                'limit': 500,
            })
            for c in camps:
                b = c.get('daily_budget') or c.get('lifetime_budget') or '0'
                try: b = float(b)
                except (TypeError, ValueError): b = 0
                if b == 0:
                    ads = (c.get('adsets') or {}).get('data', [])
                    b = sum(
                        float(a.get('daily_budget') or a.get('lifetime_budget') or 0)
                        for a in ads if a.get('effective_status') == 'ACTIVE'
                    )
                budget = b / 100
                if budget == 0:
                    continue
                product, category = classify_corrected(c['name'])
                rec = {
                    'cid': c['id'],
                    'name': c['name'],
                    'account': label,
                    'budget': budget,
                }
                raw_records[(portal, product, category)].append(rec)
                all_cids.append(c['id'])
            print(f"  {portal} {label}: {len(camps)} ACTIVE")
            time.sleep(0.4)

    # Pull audience targeting for every camp
    print(f"Fetching audience targeting for {len(set(all_cids))} unique camps...")
    aud_map = fetch_audience_map(list(set(all_cids)))

    # Annotate + bucket
    for (portal, product, category), camps in raw_records.items():
        bp = by_portal[portal][product]
        bp['cat'] = category
        for c in camps:
            c['segment'] = segment_label(aud_map.get(c['cid'], {}))
            bp['camps'].append(c)

    return by_portal


# ──────────────────────────────────────────────────────────────────────────────
# Sheet writing
# ──────────────────────────────────────────────────────────────────────────────

def open_sheet():
    sa_file = os.environ.get('GOOGLE_SERVICE_ACCOUNT_FILE')
    if not sa_file or not os.path.isfile(sa_file):
        sys.exit(f"GOOGLE_SERVICE_ACCOUNT_FILE missing or invalid: {sa_file}")
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_file(sa_file, scopes=scopes)
    return gspread.authorize(creds).open_by_key(SHEET_ID)


def write_portal_tab(sh, portal, products, ist_date_label):
    tab_title = f"📊 {portal} — Active Budget by Product {ist_date_label}"
    note_excl = " — EXCL SM_CREDIT_LINE_06" if portal == 'SM' else ""

    portal_camp_count = sum(len(p['camps']) for p in products.values())
    portal_budget = sum(c['budget'] for p in products.values() for c in p['camps'])

    rows = []
    rows.append([f"📊 {portal} — Active Budget by Product (with Segment) — {ist_date_label}{note_excl}"])
    rows.append([
        f"Snapshot of currently ACTIVE {portal} campaigns. 'Segment' = first "
        f"custom-audience name from ad-set targeting (Meta API). "
        f"'Loose' = no audience, no interests."
    ])
    rows.append([
        f"{portal_camp_count} ACTIVE camps  |  ₹{int(portal_budget):,} daily budget  "
        f"|  {len(products)} distinct products  |  Refreshed: "
        f"{datetime.now(timezone(timedelta(hours=5, minutes=30))).strftime('%H:%M IST')}"
    ])
    rows.append([])

    # Section A — rollup
    rows.append(["PRODUCT ROLLUP"])
    rows.append(["Product", "Category", "#Camps", "Daily Budget (₹)", "Share %"])
    for product, p in sorted(products.items(),
                              key=lambda kv: -sum(c['budget'] for c in kv[1]['camps'])):
        pb = sum(c['budget'] for c in p['camps'])
        share = round(pb / portal_budget * 100, 1) if portal_budget else 0
        rows.append([product, p['cat'], len(p['camps']), pb, share])
    rows.append(["TOTAL", "", portal_camp_count, portal_budget, 100.0])
    rows.append([])

    # Section B — per-camp
    rows.append(["PER-CAMP DETAIL"])
    rows.append(["Product / Camp Name", "Category", "Camp ID", "Account",
                 "Segment (Real Audience)", "Budget (₹)"])
    for product, p in sorted(products.items(),
                              key=lambda kv: -sum(c['budget'] for c in kv[1]['camps'])):
        pb = sum(c['budget'] for c in p['camps'])
        rows.append([f"▼ {product}  ({len(p['camps'])} camps)", p['cat'], "", "", "", pb])
        for c in sorted(p['camps'], key=lambda x: -x['budget']):
            rows.append(["    " + c['name'], p['cat'], c['cid'], c['account'],
                         c.get('segment', '?'), c['budget']])
    rows.append(["TOTAL", "", "", "", "", portal_budget])

    # Replace tab
    existing = {w.title: w for w in sh.worksheets()}
    if tab_title in existing:
        sh.del_worksheet(existing[tab_title])
    ws = sh.add_worksheet(title=tab_title, rows=len(rows) + 10, cols=6)
    ws.update(range_name='A1', values=rows, value_input_option='USER_ENTERED')

    # Formatting
    sheet_id = ws.id
    color = PORTAL_COLOR[portal]
    fmt = []

    # Title row in portal color
    fmt.append({'repeatCell': {
        'range': {'sheetId': sheet_id, 'startRowIndex': 0, 'endRowIndex': 1,
                  'startColumnIndex': 0, 'endColumnIndex': 6},
        'cell': {'userEnteredFormat': {
            'backgroundColor': color,
            'textFormat': {'bold': True, 'fontSize': 12}}},
        'fields': 'userEnteredFormat(backgroundColor,textFormat)'}})

    def find_row(label):
        for i, r in enumerate(rows, start=1):
            if r and isinstance(r[0], str) and r[0] == label:
                return i
        return None

    for label, ncols in [("PRODUCT ROLLUP", 5), ("PER-CAMP DETAIL", 6)]:
        r = find_row(label)
        if r is None:
            continue
        fmt.append({'repeatCell': {
            'range': {'sheetId': sheet_id, 'startRowIndex': r-1, 'endRowIndex': r,
                      'startColumnIndex': 0, 'endColumnIndex': 6},
            'cell': {'userEnteredFormat': {
                'backgroundColor': {'red': 0.85, 'green': 0.85, 'blue': 0.95},
                'textFormat': {'bold': True, 'fontSize': 11}}},
            'fields': 'userEnteredFormat(backgroundColor,textFormat)'}})
        fmt.append({'repeatCell': {
            'range': {'sheetId': sheet_id, 'startRowIndex': r, 'endRowIndex': r+1,
                      'startColumnIndex': 0, 'endColumnIndex': ncols},
            'cell': {'userEnteredFormat': {
                'backgroundColor': {'red': 0.93, 'green': 0.96, 'blue': 1.0},
                'textFormat': {'bold': True}}},
            'fields': 'userEnteredFormat(backgroundColor,textFormat)'}})

    for i, r in enumerate(rows, start=1):
        if not r:
            continue
        first = r[0] if isinstance(r[0], str) else ""
        if first == 'TOTAL':
            fmt.append({'repeatCell': {
                'range': {'sheetId': sheet_id, 'startRowIndex': i-1, 'endRowIndex': i,
                          'startColumnIndex': 0, 'endColumnIndex': 6},
                'cell': {'userEnteredFormat': {
                    'textFormat': {'bold': True, 'fontSize': 11},
                    'backgroundColor': {'red': 0.93, 'green': 0.93, 'blue': 0.93}}},
                'fields': 'userEnteredFormat(backgroundColor,textFormat)'}})
        elif first.startswith('▼ '):
            fmt.append({'repeatCell': {
                'range': {'sheetId': sheet_id, 'startRowIndex': i-1, 'endRowIndex': i,
                          'startColumnIndex': 0, 'endColumnIndex': 6},
                'cell': {'userEnteredFormat': {
                    'textFormat': {'bold': True},
                    'backgroundColor': {'red': 0.97, 'green': 0.97, 'blue': 0.97}}},
                'fields': 'userEnteredFormat(backgroundColor,textFormat)'}})

    fmt.append({'updateSheetProperties': {
        'properties': {'sheetId': sheet_id, 'gridProperties': {'frozenRowCount': 3}},
        'fields': 'gridProperties.frozenRowCount'}})
    fmt.append({'autoResizeDimensions': {
        'dimensions': {'sheetId': sheet_id, 'dimension': 'COLUMNS',
                       'startIndex': 0, 'endIndex': 6}}})

    sh.batch_update({'requests': fmt})
    return tab_title, sheet_id, len(rows), portal_camp_count, portal_budget


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────

def main():
    # IST date for tab titles (e.g. "30 Apr 26")
    ist_now = datetime.now(timezone(timedelta(hours=5, minutes=30)))
    date_label = ist_now.strftime("%d %b %y")
    print(f"Active Budget by Product report — IST date {date_label}")
    print(f"Sheet: {SHEET_ID}")
    print()

    by_portal = collect_camps()
    sh = open_sheet()

    print()
    print(f"Writing 3 per-portal tabs to {sh.title}...")
    for portal in ['SM', 'SML', 'NBP']:
        products = by_portal.get(portal, {})
        if not products:
            print(f"  {portal}: no active camps — skipping")
            continue
        title, gid, n_rows, n_camps, total_b = write_portal_tab(
            sh, portal, products, date_label
        )
        print(f"  ✓ {title}  |  {n_camps} camps, ₹{int(total_b):,} daily, {n_rows} rows  |  gid={gid}")

    print()
    print("Done.")


if __name__ == "__main__":
    main()
