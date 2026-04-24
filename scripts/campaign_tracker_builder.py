"""
Campaign Tracker Builder — Automated Sheet Creator
Replicates the SM/SML/NBP campaign tracker tabs from Meta API data.

Usage:
  python3 campaign_tracker_builder.py --account SM_FRAGRANCE_01 --date 2026-04-18
  python3 campaign_tracker_builder.py --account SM_FRAGRANCE_01  # defaults to today
  python3 campaign_tracker_builder.py --all  # runs all active accounts

Sheet: https://docs.google.com/spreadsheets/d/11IAPsJlil75aehYf5IzpSaTCLcAgPk9-57p6ZuPNNQM
"""

import os
import sys
import json
import time
import argparse
import requests
import gspread
from datetime import datetime, timedelta
from collections import defaultdict

from google.oauth2.service_account import Credentials
from dotenv import load_dotenv

# ── Load config ────────────────────────────────────────────────────────────────
load_dotenv('/Users/pulkitsharma/.openclaw/workspace/.env')

TOKEN       = os.getenv('META_ACCESS_TOKEN')
SA_FILE     = '/Users/pulkitsharma/.openclaw/workspace/google-service-account.json'
SHEET_ID    = '11IAPsJlil75aehYf5IzpSaTCLcAgPk9-57p6ZuPNNQM'
GRAPH_URL   = 'https://graph.facebook.com/v19.0'

# ── Account map: label → (act_id, tab_prefix) ─────────────────────────────────
ACCOUNT_MAP = {
    'SM_SKIN':          (os.getenv('SM_SKIN'),         'SM'),
    'SM_HAIR':          (os.getenv('SM_HAIR'),         'SM'),
    'SM_CRYSTALS':      (os.getenv('SM_CRYSTALS'),     'SM'),
    'SM_PERFUME':       (os.getenv('SM_PERFUME'),      'SM'),
    'SM_FRAGRANCE_01':  (os.getenv('SM_FRAGRANCE_01'), 'SM'),
    'SML_SKIN':         (os.getenv('SML_SKIN'),        'SML'),
    'SML_HAIR':         (os.getenv('SML_HAIR'),        'SML'),
    'SML_CRYSTALS':     (os.getenv('SML_CRYSTALS'),    'SML'),
    'SML_CL_06':        (os.getenv('SML_CL_06'),       'SML'),
    'SML_CL_07':        (os.getenv('SML_CL_07'),       'SML'),
    'NBP_SKIN':         (os.getenv('NBP_SKIN'),        'NBP'),
    'NBP_HAIR_PERFUME': (os.getenv('NBP_HAIR_PERFUME'),'NBP'),
    'NBP_CRYSTALS':     (os.getenv('NBP_CRYSTALS'),    'NBP'),
    'N129':             (os.getenv('N129'),            'N129'),
    'MONEY03':          (os.getenv('MONEY03'),         'M03'),
    'SM_CL_05':         (os.getenv('SM_CREDIT_LINE_05'), 'SM'),
    'SM_CL_06':         (os.getenv('SM_CREDIT_LINE_06'), 'SM'),
}

# ── Column headers (exact match to original sheet) ────────────────────────────
HEADERS = [
    'Account name',                          # A  — from API
    'Campaign name',                         # B  — from API
    'Attribution setting',                   # C  — from API
    'Status',                                # D  — from API
    'Start date',                            # E  — from API
    'Day Taken',                             # F  — CALCULATED: report_date - start_date + 3
    'Amount spent (INR)',                    # G  — from API
    'Roas 1 Day',                            # H  — from API (1d_click window)
    'Roas 2 days',                           # I  — from API (2d_click window)
    'Roas 3 days',                           # J  — from API (3d_click window)
    'Roas 7 days',                           # K  — from API (7d_click window)
    'Budget',                                # L  — from API (daily_budget)
    'Impressions',                           # M  — from API
    'Clicks (all)',                          # N  — from API
    'CPM (cost per 1,000 impressions)',      # O  — CALCULATED: (spend/impressions)*1000
    'CTR (ALL)',                             # P  — CALCULATED: (clicks/impressions)*100
    'Impressions/Spent',                     # Q  — CALCULATED: impressions/spend
    'Last Edied Date',                       # R  — blank (manual)
    'Days',                                  # S  — blank (manual)
    'campaign_id',                           # T  — from API
    'Column 1',                              # U  — CALCULATED: '#' + campaign_id
    'Audience Exc',                          # V  — blank (manual)
    'Type',                                  # W  — DERIVED from campaign name
    'Segment',                               # X  — lookup (campaign_id based)
    'Product',                               # Y  — lookup (campaign_id based)
    'Category',                              # Z  — blank (manual)
]


# ── Helpers ───────────────────────────────────────────────────────────────────

def meta_get(endpoint, params, retries=3):
    """GET from Meta Graph API with retry."""
    params['access_token'] = TOKEN
    for attempt in range(retries):
        try:
            r = requests.get(f"{GRAPH_URL}/{endpoint}", params=params, timeout=30)
            data = r.json()
            if 'error' in data:
                err = data['error']
                print(f"  ⚠️  Meta API error: {err.get('message')} (code={err.get('code')})")
                if err.get('code') == 17:  # rate limit
                    time.sleep(60)
                    continue
                return None
            return data
        except Exception as e:
            print(f"  ⚠️  Request error (attempt {attempt+1}): {e}")
            time.sleep(5)
    return None


def paginate(endpoint, params):
    """Fetch all pages from a Meta API endpoint."""
    results = []
    data = meta_get(endpoint, params)
    if not data:
        return results
    results.extend(data.get('data', []))
    while 'paging' in data and 'next' in data['paging']:
        try:
            r = requests.get(data['paging']['next'], timeout=30)
            data = r.json()
            results.extend(data.get('data', []))
        except:
            break
    return results


# ── Use product_catalogue.py for all derivations ──────────────────────────────
from product_catalogue import (
    derive_type,
    derive_segment,
    derive_product_and_category,
)

def _legacy_derive_category(campaign_name):
    """Derive Category (Skin / Hair / Crystal / Jewellery / Neutra / Fragrance / Clock / Frame) from campaign name."""
    n = campaign_name.lower()
    if any(k in n for k in ['skin', 'serum', 'triderma', 'ampm', 'am_pm', 'am pm', 'pitglow', 'pit_glow', 'pit glow',
                             'gold_serum', '24k', 'goat_milk', 'goatmilk', 'trifecta', 'time_reversal',
                             'sunkissed', 'roll_on', 'roll on', 'lipbright', 'lip_bright', 'berberine',
                             'charbigone', 'charbi_gone', 'neutra']):
        if any(k in n for k in ['charbigone', 'charbi_gone', 'neutra', 'berberine', 'capsule']):
            return 'Nutraceuticals'
        return 'Skin'
    if any(k in n for k in ['hair', 'xtreme', 'phusphus', 'phus_phus', 'phus phus', 'mist', 'xtremehair']):
        return 'Hair'
    if any(k in n for k in ['crystal', 'selenite', 'pyrite', 'hourglass', 'peacock', 'richie_rich', 'richierich',
                             'horses', 'ganesha', 'hanuman', 'money_bowl', 'coaster', 'miniature', 'half_n_half']):
        return 'Crystal'
    if any(k in n for k in ['jewellery', 'jewelry', 'nazar_sutra', 'nazar sutra', 'prem_sutra', 'prem sutra',
                             'bracelet', 'wanda']):
        return 'Jewellery'
    if any(k in n for k in ['clock', 'clocks']):
        return 'Clocks'
    if any(k in n for k in ['frame', 'frames']):
        return 'Frames'
    if any(k in n for k in ['fragrance', 'perfume', 'scent']):
        return 'Fragrance'
    return ''


def derive_product(campaign_name):
    """Derive Product from campaign name keywords."""
    n = campaign_name.lower()
    product_map = [
        (['am_pm', 'ampm', 'am pm', 'booster_mix', 'booster mix'],   'AM/PM Booster Kit'),
        (['triderma'],                                                  'Triderma Bright'),
        (['time_reversal', 'time reversal', 'trifecta'],               'Time Reversal Trifecta'),
        (['24k', 'gold_serum', 'gold serum'],                          '24K Gold Face Serum'),
        (['goat_milk', 'goatmilk', 'goat milk'],                       'Under Eye Goat Milk'),
        (['pitglow', 'pit_glow', 'pit glow'],                          'Pit Glow Roll On'),
        (['sunkissed', 'sun_kissed'],                                   'Sun Kissed Mousse'),
        (['xtremehair', 'xtreme_hair', 'xtreme hair', 'hair.*booster'],'Xtreme Hair Booster Kit'),
        (['phusphus', 'phus_phus', 'phus phus', 'hair.*mist'],         'Phus Phus Hair Mist'),
        (['pyrite.*bracelet', 'bracelet.*pyrite', 'bracelet'],          'Pyrite Bracelet'),
        (['prem_sutra', 'prem sutra'],                                  'Prem Sutra Bracelet'),
        (['nazar_sutra', 'nazar sutra'],                                'Nazar Sutra'),
        (['selenite.*coaster', 'coaster'],                              'Selenite Coaster'),
        (['hourglass'],                                                  'Crystal Hourglass'),
        (['7_horse', '7 horse', 'richie_rich', 'richierich'],           '7 Horses Frame'),
        (['peacock'],                                                    'Peacock Frame'),
        (['hanuman'],                                                    'Hanuman Crystal'),
        (['ganesha'],                                                    'Ganesha Crystal'),
        (['money_bowl', 'money bowl'],                                   'Money Bowl'),
        (['charbigone', 'charbi_gone', 'charbi gone'],                  'Charbi Gone Capsules'),
        (['berberine'],                                                   'Berberine Capsules'),
        (['miniature'],                                                   'Crystal Miniature'),
    ]
    import re
    for keywords, product in product_map:
        for kw in keywords:
            if re.search(kw, n):
                return product
    return ''


def derive_segment(campaign_name, audience_id=''):
    """Derive Segment from campaign name."""
    n = campaign_name.lower()
    if 'seg13' in n or 'loyal' in n or 'loyal_buyers' in n:
        return 'NTN | Loyal Buyers 3+'
    if 'seg12' in n or 'repeat' in n:
        return 'NTN | Repeat Buyers 2+'
    if 'seg11' in n:
        return 'NTN | Recent 0-30D'
    if 'seg6' in n or 'sm_only' in n or 'sm only' in n:
        return 'NTN | SM Only Buyers'
    if 'seg4' in n or '365' in n or 'lapsed' in n:
        return 'NTN | Crystal Buyers — 365+ Days (Lapsed)'
    if 'seg2' in n or '30_to_180' in n or '30to180' in n:
        return 'NTN | Crystal Buyers — 30 to 180 Days'
    if 'seg16' in n:
        return 'NTN | SM Only Buyers'
    if 'seg1' in n:
        return 'NTN | Recent 0-30D'
    if 'recent_31' in n or 'recent 31' in n or '31-60' in n:
        return 'NTN | Recent 31-60D'
    if 'xtreme_hair' in n or 'xtremehair' in n:
        return 'NTN | Product: Xtreme Hair (1+)'
    if 'lip_bright' in n or 'pit_glow' in n or 'lipbright' in n:
        return 'NTN | Lip Bright + Pit Glow Buyers'
    if 'crystal_buyer' in n or 'crystal buyer' in n:
        return 'NTN | Crystal Buyers — 30 to 180 Days'
    if 'ntn' in n and ('retarget' in n or 'rtg' in n or 'rgt' in n):
        return 'NTN | Recent 0-30D'
    if 'sale_30' in n or 'sale30' in n:
        return 'Sale 30'
    if 'visitor_180' in n or 'visitor 180' in n or '180_day' in n:
        return 'Visitor 180 Day'
    if 'visitor_30' in n or 'visitor 30' in n or '30_day' in n:
        return 'Visitor 30 Day'
    if 'impression_30' in n or 'impression 30' in n or 'imp_30' in n or '180imp' in n:
        return 'Impression 30 retarget'
    if 'impression_7' in n or 'imp_7' in n:
        return 'Impression 7 retarget'
    if 'add_to_cart' in n or 'atc' in n or 'add to cart' in n:
        if '7' in n:
            return 'ADD to cart 7 days'
        return '30 days add to cart'
    if 'lla' in n or 'lookalike' in n:
        return 'Lookalike (IN, 1%) - prepaid purchase'
    if 'c1_c4' in n or 'c9' in n:
        return 'C1_C4 _Meta'
    if 'ds' in n.split('_')[:2]:
        return 'DS'
    if 'lifetime' in n:
        return 'Lifetime audience'
    if 'loose' in n:
        return 'Loose'
    # Default for cold traffic / sales
    if derive_type(campaign_name) == 'Sales':
        return 'Lifetime audience'
    return ''


def format_date(date_str):
    """Normalize date string to DD-Mon-YYYY format."""
    if not date_str:
        return ''
    for fmt in ['%Y-%m-%dT%H:%M:%S%z', '%Y-%m-%dT%H:%M:%S+0000', '%Y-%m-%d']:
        try:
            dt = datetime.strptime(date_str[:19], fmt[:len(fmt)])
            return dt.strftime('%d-%b-%Y')
        except:
            continue
    return date_str[:10]


def calc_day_taken(start_str, report_date):
    """Day Taken = report_date - start_date + 3 (matches observed formula)."""
    for fmt in ['%d-%b-%Y', '%Y-%m-%d']:
        try:
            start = datetime.strptime(start_str, fmt)
            return (report_date - start).days + 3
        except:
            continue
    return ''


def safe_float(val, decimals=2):
    """Round float safely."""
    try:
        return round(float(val), decimals)
    except:
        return ''


def safe_div(a, b, decimals=2):
    try:
        if float(b) == 0:
            return ''
        return round(float(a) / float(b), decimals)
    except:
        return ''


# ── Lookup table for Segment + Product ───────────────────────────────────────
# Load from lookup file if it exists, else return empty
LOOKUP_FILE = '/Users/pulkitsharma/.openclaw/workspace/campaign_lookup.json'

def load_lookup():
    if os.path.exists(LOOKUP_FILE):
        with open(LOOKUP_FILE) as f:
            return json.load(f)
    return {}


def get_lookup_values(campaign_id, campaign_name, lookup):
    """Get Segment + Product from lookup, fallback to name parsing."""
    if campaign_id in lookup:
        entry = lookup[campaign_id]
        return entry.get('segment', ''), entry.get('product', '')
    # Try name-based segment hint
    name_lower = campaign_name.lower()
    segment = ''
    if 'lifetime' in name_lower:
        segment = 'Lifetime audience'
    elif 'sale_30' in name_lower or 'sale30' in name_lower:
        segment = 'Sale 30'
    elif 'visitor' in name_lower:
        segment = 'Visitor 180 Day'
    return segment, ''


# ── Core: fetch campaigns from Meta API ──────────────────────────────────────

def fetch_campaigns(account_id, date_str):
    """
    Fetch all campaigns for an account with spend > 0 for the given date.
    Returns list of dicts with all fields needed for the sheet.
    """
    print(f"\n📡 Fetching campaigns for {account_id} on {date_str}...")
    
    # Step 1: Get campaign list with basic info
    campaigns_raw = paginate(
        f"{account_id}/campaigns",
        {
            'fields': 'id,name,status,start_time,daily_budget,lifetime_budget,attribution_spec,adsets{daily_budget,lifetime_budget}',
            'effective_status': '["ACTIVE","PAUSED","CAMPAIGN_PAUSED","ADSET_PAUSED"]',
            'limit': 500,
        }
    )
    print(f"  Found {len(campaigns_raw)} campaigns (active/paused)")
    
    if not campaigns_raw:
        return []
    
    campaign_ids = [c['id'] for c in campaigns_raw]
    campaign_map = {c['id']: c for c in campaigns_raw}
    
    # Step 2: Get insights for all campaigns — multiple attribution windows
    # We need 1d, 2d, 3d, 7d ROAS separately
    # Meta supports action_attribution_windows as a list; returns breakdown

    # ROAS columns definition (matches original sheet):
    # H = 1d_click attribution  (yesterday only, 1d click window)
    # I = 2d cumulative         (last 2 days spend vs revenue)
    # J = 3d cumulative         (last 3 days spend vs revenue)
    # K = 7d_click attribution  (yesterday only, 7d click window)
    roas_by_days = {1: {}, 2: {}, 3: {}, 7: {}}
    base_metrics = {}
    revenue_cache = {}

    date_obj = datetime.strptime(date_str, '%Y-%m-%d')
    date_fmt  = date_obj.strftime('%Y-%m-%d')

    # ROAS formula (matches original sheet exactly):
    # H (1D) = 1d_click attribution, date range = report_date only
    # I (2D) = 1d_click attribution, date range = last 2 days
    # J (3D) = 1d_click attribution, date range = last 3 days
    # K (7D) = 7d_click attribution, date range = last 7 days

    def fetch_roas_window(since_date, until_date, attr_window, days_key, is_base=False):
        """Pull insights for a date range with specific attribution window."""
        rows = paginate(f"{account_id}/insights", {
            'level': 'campaign',
            'fields': 'campaign_id,campaign_name,account_name,spend,impressions,clicks,purchase_roas',
            'action_attribution_windows': json.dumps([attr_window]),
            'time_range': json.dumps({'since': since_date.strftime('%Y-%m-%d'), 'until': until_date.strftime('%Y-%m-%d')}),
            'limit': 500,
        })
        print(f"  [{days_key}D {attr_window}] Got {len(rows)} rows")
        for row in rows:
            cid   = row.get('campaign_id', '')
            spend = float(row.get('spend', 0) or 0)
            if spend == 0:
                continue
            if is_base:
                base_metrics[cid] = {
                    'campaign_name': row.get('campaign_name', ''),
                    'account_name':  row.get('account_name',  ''),
                    'spend':         row.get('spend',       '0'),
                    'impressions':   row.get('impressions', '0'),
                    'clicks':        row.get('clicks',      '0'),
                }
            # Extract ROAS from purchase_roas field
            for roas_entry in row.get('purchase_roas', []):
                if roas_entry.get('action_type') == 'omni_purchase':
                    v = roas_entry.get(attr_window, '')
                    if v:
                        roas_by_days[days_key][cid] = round(float(v), 2)
            # Cache for reports
            roas_val = roas_by_days[days_key].get(cid, 0)
            revenue_cache.setdefault(cid, {})
            revenue_cache[cid][f'spend_{days_key}d'] = spend
            revenue_cache[cid][f'rev_{days_key}d']   = round(float(roas_val) * spend, 2) if roas_val and roas_val != '-' else 0

    # Pull all 4 windows
    fetch_roas_window(date_obj,                        date_obj,                        '1d_click', 1, is_base=True)
    fetch_roas_window(date_obj - timedelta(days=1),    date_obj,                        '1d_click', 2)
    fetch_roas_window(date_obj - timedelta(days=2),    date_obj,                        '1d_click', 3)
    fetch_roas_window(date_obj - timedelta(days=6),    date_obj,                        '7d_click', 7)

    # Save revenue cache to disk for reports script
    import json as _json
    cache_path = f'/tmp/tracker_cache_{account_id}_{date_str}.json'
    with open(cache_path, 'w') as f:
        _json.dump(revenue_cache, f)
    print(f"  💾 Revenue cache saved: {cache_path}")
    
    # Step 3: Build result rows
    lookup = load_lookup()
    results = []
    
    for camp in campaigns_raw:
        cid = camp['id']
        metrics = base_metrics.get(cid, {})
        
        spend = float(metrics.get('spend', 0) or 0)
        impressions = int(metrics.get('impressions', 0) or 0)
        clicks = int(metrics.get('clicks', 0) or 0)
        
        # Skip campaigns with zero spend
        if spend == 0:
            continue
        
        name = camp.get('name', metrics.get('campaign_name', ''))
        account_name = metrics.get('account_name', '')
        status = camp.get('status', '').lower()
        if status in ('active',):
            status = 'active'
        else:
            status = 'inactive'
        
        # Start date
        start_raw = camp.get('start_time', '')
        start_formatted = format_date(start_raw)
        
        # Budget — campaign level first, then sum adset budgets (CBO campaigns)
        budget_raw = camp.get('daily_budget', '') or camp.get('lifetime_budget', '')
        if not budget_raw or budget_raw == '0':
            # CBO: sum adset daily budgets
            adsets = camp.get('adsets', {}).get('data', [])
            adset_total = sum(
                float(a.get('daily_budget', 0) or a.get('lifetime_budget', 0) or 0)
                for a in adsets
            )
            budget_raw = str(int(adset_total)) if adset_total > 0 else ''
        try:
            budget = f"{float(budget_raw)/100:,.2f}" if budget_raw else ''
        except:
            budget = ''
        
        # Attribution setting
        attr_spec = camp.get('attribution_spec', [])
        if attr_spec:
            parts = []
            for spec in attr_spec:
                window = spec.get('window_days', '')
                event = spec.get('event_type', '')
                if event == 'CLICK_THROUGH':
                    parts.append(f"{window}-day click")
                elif event == 'VIEW_THROUGH':
                    parts.append(f"{window}-day view")
                elif event == 'ENGAGED_VIEW':
                    parts.append(f"{window}-day engaged view")
            attribution = ', '.join(parts) if parts else '7-day click, 1-day view or 1-day engaged view'
        else:
            attribution = '7-day click, 1-day view or 1-day engaged view'
        
        def roas_val(days):
            v = roas_by_days.get(days, {}).get(cid, '')
            if v != '' and v != '-':
                return v
            return '-'
        
        # Calculated metrics
        cpm = safe_float(spend / impressions * 1000, 10) if impressions > 0 else ''
        ctr = safe_float(clicks / impressions * 100, 2) if impressions > 0 else ''
        imp_per_spent = safe_float(impressions / spend, 2) if spend > 0 else ''
        
        # Day taken
        day_taken = calc_day_taken(start_formatted, date_obj) if start_formatted else ''
        
        # Type + Segment + Product + Category — all from product_catalogue
        camp_type = derive_type(name)
        product, category = derive_product_and_category(name)
        segment  = derive_segment(name)
        # Lookup override (manual corrections)
        if cid in lookup:
            segment  = lookup[cid].get('segment',  segment)
            product  = lookup[cid].get('product',  product)
            category = lookup[cid].get('category', category)
        
        results.append([
            account_name,                    # A
            name,                            # B
            attribution,                     # C
            status,                          # D
            start_formatted,                 # E
            day_taken,                       # F
            round(spend, 0),                 # G
            roas_val(1),                     # H  1D ROAS
            roas_val(2),                     # I  2D ROAS
            roas_val(3),                     # J  3D ROAS
            roas_val(7),                     # K  7D ROAS
            budget,                          # L
            impressions,                     # M
            clicks,                          # N
            cpm,                             # O
            ctr,                             # P
            imp_per_spent,                   # Q
            '',                              # R Last Edited Date (blank)
            '',                              # S Days (blank)
            cid,                             # T campaign_id
            f'#{cid}',                       # U Column 1
            '',                              # V Audience Exc (blank)
            camp_type,                       # W Type
            segment,                         # X Segment
            product,                         # Y Product
            category,                        # Z Category (derived)
        ])
    
    print(f"  ✅ Processed {len(results)} campaigns with spend")
    return results


# ── Write to Google Sheet ─────────────────────────────────────────────────────

def write_to_sheet(rows, account_key, date_str, tab_prefix):
    """Create or overwrite a tab in the campaign tracker sheet."""
    date_obj = datetime.strptime(date_str, '%Y-%m-%d')
    # Tab name format: "SM 17 APR 26"
    tab_name = f"{tab_prefix} {date_obj.day} {date_obj.strftime('%b').upper()} {date_obj.strftime('%y')}"
    
    print(f"\n📊 Writing to sheet tab: '{tab_name}'...")
    
    SCOPES = ['https://www.googleapis.com/auth/spreadsheets']
    creds = Credentials.from_service_account_file(SA_FILE, scopes=SCOPES)
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(SHEET_ID)
    
    # Always: clear and write fresh — no appending, no duplicates
    existing_titles = [ws.title for ws in sh.worksheets()]
    if tab_name in existing_titles:
        ws = sh.worksheet(tab_name)
        ws.clear()
        print(f"  Tab '{tab_name}' cleared — writing {len(rows)} rows fresh...")
    else:
        ws = sh.add_worksheet(title=tab_name, rows=max(500, len(rows)+10), cols=26)
        sh.reorder_worksheets([ws] + [w for w in sh.worksheets() if w.title != tab_name])
        print(f"  Creating new tab '{tab_name}' — writing {len(rows)} rows...")

    all_data = [HEADERS] + rows
    ws.update(values=all_data, range_name='A1', value_input_option='USER_ENTERED')
    
    # Format header row — bold
    try:
        ws.format('A1:Z1', {
            'textFormat': {'bold': True},
            'backgroundColor': {'red': 0.2, 'green': 0.2, 'blue': 0.6}
        })
    except:
        pass
    
    print(f"  ✅ Written {len(rows)} rows to '{tab_name}'")
    return tab_name


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='Campaign Tracker Builder')
    parser.add_argument('--account', help='Account key (e.g. SM_FRAGRANCE_01)')
    parser.add_argument('--date', help='Date YYYY-MM-DD (default: today)')
    parser.add_argument('--all', action='store_true', help='Run all accounts')
    parser.add_argument('--dry-run', action='store_true', help='Fetch only, no sheet write')
    args = parser.parse_args()
    
    date_str = args.date or datetime.now().strftime('%Y-%m-%d')
    
    if args.all:
        accounts = list(ACCOUNT_MAP.keys())
    elif args.account:
        accounts = [args.account]
    else:
        parser.print_help()
        sys.exit(1)
    
    print(f"\n🚀 Campaign Tracker Builder")
    print(f"   Date: {date_str}")
    print(f"   Accounts: {accounts}")
    
    results_summary = []

    # Collect ALL rows per portal first — then write each portal tab ONCE
    # This prevents duplicates from multiple accounts writing to the same tab
    portal_rows = defaultdict(dict)  # portal → {cid: row}  (dedup by CID, last write wins)

    for acct_key in accounts:
        if acct_key not in ACCOUNT_MAP:
            print(f"⚠️  Unknown account: {acct_key}")
            continue

        act_id, tab_prefix = ACCOUNT_MAP[acct_key]
        if not act_id:
            print(f"⚠️  No act_id for {acct_key}")
            continue

        try:
            rows = fetch_campaigns(act_id, date_str)

            if not rows:
                print(f"  ⚠️  No data for {acct_key}")
                results_summary.append((acct_key, 0, 'no data'))
                continue

            if args.dry_run:
                print(f"\n🔍 DRY RUN — {len(rows)} rows for {acct_key}:")
                for r in rows[:3]:
                    print(f"  {r[1][:50]} | Spent={r[6]} | 1D={r[7]} | 7D={r[10]} | Type={r[22]}")
                results_summary.append((acct_key, len(rows), 'dry-run'))
            else:
                # Collect into portal bucket, keyed by campaign_id (col 19) to prevent dupes
                for r in rows:
                    cid = r[19]  # campaign_id column
                    portal_rows[tab_prefix][cid] = r
                results_summary.append((acct_key, len(rows), f'→ {tab_prefix} (queued)'))

        except Exception as e:
            print(f"  ❌ Error for {acct_key}: {e}")
            import traceback; traceback.print_exc()
            results_summary.append((acct_key, 0, f'error: {e}'))

        time.sleep(2)

    # Write each portal tab ONCE with all deduplicated rows
    if not args.dry_run:
        for tab_prefix, cid_row_map in portal_rows.items():
            all_rows = list(cid_row_map.values())
            # Sort by account name then campaign name for clean ordering
            all_rows.sort(key=lambda r: (r[0], r[1]))
            tab_name = write_to_sheet(all_rows, tab_prefix, date_str, tab_prefix)
            print(f"  ✅ {tab_prefix}: {len(all_rows)} unique campaigns written to '{tab_name}'")
            # Update summary
            for i, (acct, cnt, status) in enumerate(results_summary):
                if '→' + f' {tab_prefix}' in status:
                    results_summary[i] = (acct, cnt, tab_name)

    print("\n\n=== SUMMARY ===")
    for acct, count, status in results_summary:
        print(f"  {acct}: {count} rows → {status}")


if __name__ == '__main__':
    main()
