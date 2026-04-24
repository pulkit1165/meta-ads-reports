#!/usr/bin/env python3
"""
Closing Watchlist — 3-section report in Campaign Tracker sheet
Tab: 🔴 Closing DD MMM YY

Section 1: 🚨 Critical  — camps under 1x ROAS by age bucket
Section 2: ⚠️ Warning   — age-based thresholds (Day 7+<2x, 4-7<2x, 3<1.5x, 2<1.5x, 1<1.5x)
Section 3: 📊 Budget    — Day bucket × Campaign Type (Sales/Retarget/TOF) with ROAS

Runs: 10AM / 12PM / 3PM / 6PM IST daily
Portal: SM only (Studd Muffyn)

Usage:
  python3 closing_watchlist.py
  python3 closing_watchlist.py --date 2026-04-24
"""

import os, sys, json, argparse, requests, time
from datetime import datetime, timedelta
from collections import defaultdict
from zoneinfo import ZoneInfo

import gspread
from google.oauth2.service_account import Credentials
from dotenv import load_dotenv

load_dotenv('/Users/pulkitsharma/.openclaw/workspace/.env')

TOKEN    = os.getenv('META_ACCESS_TOKEN')
SA_FILE  = '/Users/pulkitsharma/.openclaw/workspace/google-service-account.json'
SHEET_ID = '11IAPsJlil75aehYf5IzpSaTCLcAgPk9-57p6ZuPNNQM'
SCOPES   = ['https://www.googleapis.com/auth/spreadsheets']
GRAPH    = 'https://graph.facebook.com/v19.0'
IST      = ZoneInfo('Asia/Kolkata')

# Snapshot file: stores CIDs from previous run for closed-camp detection
def snapshot_path(date_str):
    return f'/tmp/closing_snapshot_{date_str}.json'

def load_snapshot(date_str):
    path = snapshot_path(date_str)
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {}   # {cid: {'name': ..., 'roas': ..., 'budget': ..., 'run_label': ...}}

def save_snapshot(date_str, camps, run_label):
    path = snapshot_path(date_str)
    existing = load_snapshot(date_str)
    for c in camps:
        existing[c['cid']] = {
            'name':         c['name'],
            'roas':         c['roas'],
            'roas_yday':    c.get('roas_yday'),
            'roas_7d':      c.get('roas_7d'),
            'budget':       c['budget'],
            'camp_type':    c['camp_type'],
            'bucket_label': c['bucket_label'],
            'run_label':    run_label,
        }
    with open(path, 'w') as f:
        json.dump(existing, f)

SM_ACCOUNTS = [
    (os.getenv('SM_FRAGRANCE_01'),   'SM Fragrance 01'),
    (os.getenv('SM_SKIN'),           'SM Skin'),
    (os.getenv('SM_HAIR'),           'SM Hair'),
    (os.getenv('SM_CRYSTALS'),       'SM Crystals'),
    (os.getenv('SM_PERFUME'),        'SM Perfume'),
    (os.getenv('SM_CREDIT_LINE_05'), 'SM CL 05'),
    (os.getenv('SM_CREDIT_LINE_06'), 'SM CL 06'),
]

# ── Thresholds ────────────────────────────────────────────────────────────────
CRITICAL_THRESHOLD = {
    'day0': 1.0, 'day1': 1.0, 'day2': 1.0,
    'day3': 1.0, 'day4_7': 1.0, 'day7p': 1.0,
}
WARNING_THRESHOLD = {
    'day1': 1.5, 'day2': 1.5, 'day3': 1.5,
    'day4_7': 2.0, 'day7p': 2.0,
}

# ── Helpers ───────────────────────────────────────────────────────────────────

def safe_float(v):
    try: return float(str(v).replace(',', '').strip())
    except: return 0.0

def paginate(endpoint, params, retries=3):
    p = dict(params); p['access_token'] = TOKEN
    results = []
    for attempt in range(retries):
        try:
            r = requests.get(f"{GRAPH}/{endpoint}", params=p, timeout=30)
            data = r.json()
            if 'error' in data:
                if data['error'].get('code') == 17:
                    print("  ⏳ Rate limit..."); time.sleep(60); continue
                print(f"  ⚠️  {data['error'].get('message','API error')}")
                return results
            results.extend(data.get('data', []))
            while data.get('paging', {}).get('next'):
                r2 = requests.get(data['paging']['next'], timeout=30)
                data = r2.json()
                results.extend(data.get('data', []))
            return results
        except Exception as e:
            print(f"  ⚠️  Request error ({attempt+1}): {e}"); time.sleep(5)
    return results

def extract_roas(raw, prefer_key='value'):
    """
    Extract ROAS from Meta purchase_roas field.
    prefer_key='value'   → default attribution (matches campaign tracker sheet)
    prefer_key='1d_click' → strict 1-day click only
    Falls back to the other key if preferred is 0.
    """
    if not raw: return None
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                try:
                    v = item.get(prefer_key)
                    fallback = item.get('1d_click') if prefer_key == 'value' else item.get('value')
                    val = float(v) if v not in (None, '', '0', 0) else (float(fallback) if fallback not in (None, '', '0', 0) else 0.0)
                    return round(val, 2)
                except: continue
    return None

def age_info(start_str, today_dt):
    """Returns (age_days, bucket_key, bucket_label)."""
    if not start_str:
        return 0, 'day0', 'Day 0'
    try:
        start = datetime.strptime(start_str[:10], '%Y-%m-%d').date()
        days  = max(0, (today_dt - start).days)
        if days == 0:   return 0, 'day0',   '🆕 Day 0'
        if days == 1:   return 1, 'day1',   '📅 Day 1'
        if days == 2:   return 2, 'day2',   '📅 Day 2'
        if days == 3:   return 3, 'day3',   '📅 Day 3'
        if days <= 7:   return days, 'day4_7', '📆 Day 4–7'
        return days, 'day7p', f'⏳ Day 7+ ({days}d)'
    except:
        return 0, 'day0', '🆕 Day 0'

def campaign_type(name):
    """Detect Sales / Retarget / TOF from campaign name."""
    n = name.lower()
    if any(k in n for k in ['tof', '_tof_', 'top_of_funnel', 'interest', 'loose', '_broad', 'cold']):
        return 'TOF'
    if any(k in n for k in ['rtg', 'rgt', 'retarget', '_seg', 'seg_', 'visitor', 'impression_',
                              'imp_', 'atc', 'add_to_cart', 'cart', 'bof', 'c1_c4', 'ds_bof',
                              'lla', 'lookalike', 'ntn', '180dp', '180imp', 'exc30']):
        return 'Retarget'
    return 'Sales'

def bucket_order(key):
    return {'day0':0,'day1':1,'day2':2,'day3':3,'day4_7':4,'day7p':5}.get(key, 6)

# ── Fetch live data ───────────────────────────────────────────────────────────

def fetch_sm_data(date_str):
    """
    Returns list of camp dicts with live ROAS, yesterday ROAS, 7D max ROAS, budget, spend, age.
    Uses default attribution (value key) to match campaign tracker sheet.
    """
    today_dt  = datetime.strptime(date_str, '%Y-%m-%d').date()
    yesterday = (today_dt - timedelta(days=1)).strftime('%Y-%m-%d')
    since_7d  = (today_dt - timedelta(days=6)).strftime('%Y-%m-%d')
    camps_out = []

    for account_id, acct_name in SM_ACCOUNTS:
        if not account_id: continue

        # Today's insights
        rows_1d = paginate(f"{account_id}/insights", {
            'level':  'campaign',
            'fields': 'campaign_id,spend,purchase_roas',
            'action_attribution_windows': json.dumps(['1d_click']),
            'time_range': json.dumps({'since': date_str, 'until': date_str}),
            'filtering': json.dumps([{'field':'spend','operator':'GREATER_THAN','value':'0'}]),
            'limit': 500,
        })

        roas_map  = {}
        spend_map = {}
        for r in rows_1d:
            cid = r.get('campaign_id', '')
            roas_map[cid]  = extract_roas(r.get('purchase_roas'), prefer_key='value')
            spend_map[cid] = safe_float(r.get('spend', 0))

        # Yesterday ROAS
        rows_yday = paginate(f"{account_id}/insights", {
            'level':  'campaign',
            'fields': 'campaign_id,purchase_roas',
            'action_attribution_windows': json.dumps(['1d_click']),
            'time_range': json.dumps({'since': yesterday, 'until': yesterday}),
            'filtering': json.dumps([{'field':'spend','operator':'GREATER_THAN','value':'0'}]),
            'limit': 500,
        })
        roas_yday = {}
        for r in rows_yday:
            cid = r.get('campaign_id', '')
            roas_yday[cid] = extract_roas(r.get('purchase_roas'), prefer_key='value')

        # 7D ROAS (max window)
        rows_7d = paginate(f"{account_id}/insights", {
            'level':  'campaign',
            'fields': 'campaign_id,purchase_roas',
            'action_attribution_windows': json.dumps(['7d_click']),
            'time_range': json.dumps({'since': since_7d, 'until': date_str}),
            'filtering': json.dumps([{'field':'spend','operator':'GREATER_THAN','value':'0'}]),
            'limit': 500,
        })
        roas_7d = {}
        for r in rows_7d:
            cid = r.get('campaign_id', '')
            roas_7d[cid] = extract_roas(r.get('purchase_roas'), prefer_key='value')

        # Active campaigns metadata
        camps_raw = paginate(f"{account_id}/campaigns", {
            'fields': 'id,name,status,start_time,daily_budget,lifetime_budget,adsets{daily_budget,status}',
            'effective_status': '["ACTIVE"]',
            'limit': 500,
        })

        for c in camps_raw:
            cid = c['id']
            if cid not in spend_map:
                continue  # no spend today

            roas  = roas_map.get(cid)   # None = has spend but 0 purchases
            spend = spend_map[cid]
            start = c.get('start_time', '')[:10]

            # Budget
            cbo = float(c.get('daily_budget') or 0)
            if cbo > 0:
                budget = cbo / 100
            else:
                adsets = c.get('adsets', {}).get('data', [])
                budget = sum(float(a.get('daily_budget') or 0) / 100
                             for a in adsets if a.get('status') == 'ACTIVE')

            age_days, bucket_key, bucket_label = age_info(start, today_dt)
            camp_type  = campaign_type(c['name'])
            roas_val   = roas if roas is not None else 0.0
            is_zero    = (roas is None or roas == 0.0)
            roas_y     = roas_yday.get(cid)    # yesterday ROAS
            roas_7     = roas_7d.get(cid)      # 7D max ROAS
            # Was above threshold yesterday?
            above_yday = (roas_y is not None and roas_y >= 1.0)
            # Max ROAS above threshold?
            max_roas   = max(r for r in [roas_val, roas_y or 0, roas_7 or 0])

            camps_out.append({
                'cid':          cid,
                'name':         c['name'],
                'account':      acct_name,
                'budget':       budget,
                'spend':        spend,
                'roas':         roas_val,
                'roas_yday':    roas_y,
                'roas_7d':      roas_7,
                'max_roas':     max_roas,
                'above_yday':   above_yday,
                'is_zero_roas': is_zero,
                'start':        start,
                'age_days':     age_days,
                'bucket_key':   bucket_key,
                'bucket_label': bucket_label,
                'camp_type':    camp_type,
            })

        print(f"  {acct_name}: {len([r for r in rows_1d])} camps with spend")

    return camps_out

# ── Build sheet sections ──────────────────────────────────────────────────────

def pct(spent, budget):
    if budget == 0: return '—'
    return f"{round(spent/budget*100)}%"

def fmt_roas(r):
    if r == 0: return '0x ⚫'
    return f"{r}x"

def build_critical_section(camps, update_str):
    """Section 1: All camps under 1x ROAS, bucketed."""
    critical = [c for c in camps if c['roas'] < 1.0]

    rows = []

    # Section header
    total_bud   = sum(c['budget'] for c in critical)
    total_spend = sum(c['spend']  for c in critical)
    zero_bud    = sum(c['budget'] for c in critical if c['is_zero_roas'])
    rows.append([f'🚨  CRITICAL CLOSES — Under 1x ROAS  |  {update_str}  |  {len(critical)} camps  |  Budget: ₹{total_bud:,.0f}  |  Spent: ₹{total_spend:,.0f} ({pct(total_spend,total_bud)})  |  Zero ROAS Budget: ₹{zero_bud:,.0f}',
                 '', '', '', '', '', '', ''])

    # Bucket summary
    rows.append(['Age Bucket', 'Camps', 'Budget (₹)', '% Spent', 'Spent (₹)', 'Zero ROAS Camps', 'Zero ROAS Budget (₹)', 'Notes'])

    bucket_order_list = ['day0','day1','day2','day3','day4_7','day7p']
    buckets = defaultdict(list)
    for c in critical:
        buckets[c['bucket_key']].append(c)

    for bk in bucket_order_list:
        bc = buckets[bk]
        if not bc: continue
        bud   = sum(c['budget'] for c in bc)
        spnd  = sum(c['spend']  for c in bc)
        z_cnt = sum(1 for c in bc if c['is_zero_roas'])
        z_bud = sum(c['budget'] for c in bc if c['is_zero_roas'])
        label = bc[0]['bucket_label']
        rows.append([label, len(bc), int(bud), pct(spnd, bud), int(spnd), z_cnt, int(z_bud), ''])

    rows.append(['TOTAL', len(critical), int(total_bud), pct(total_spend, total_bud), int(total_spend), sum(1 for c in critical if c['is_zero_roas']), int(zero_bud), ''])
    rows.append([''])

    # Detailed camp list per bucket
    rows.append(['Bucket', 'Campaign Name', 'Type', 'Budget (₹)', 'Spent (₹)', '% Spent',
                 'Today ROAS', 'Yday ROAS', '7D ROAS', '⚠️ Flag', 'Started'])
    for bk in bucket_order_list:
        bc = sorted(buckets[bk], key=lambda x: -x['budget'])
        for c in bc:
            # Flag: was above 1 yesterday or max ROAS > 1 (potential good camp having bad day)
            flag = ''
            if c['above_yday']:
                flag = f"✅ Was {c['roas_yday']}x yday"
            elif c['max_roas'] >= 1.5:
                flag = f"📈 Max {c['max_roas']}x"
            rows.append([
                c['bucket_label'],
                c['name'],
                c['camp_type'],
                int(c['budget']),
                int(c['spend']),
                pct(c['spend'], c['budget']),
                fmt_roas(c['roas']),
                fmt_roas(c['roas_yday']) if c['roas_yday'] is not None else '—',
                fmt_roas(c['roas_7d'])   if c['roas_7d']   is not None else '—',
                flag,
                c['start'],
            ])

    rows.append([''])
    return rows


def build_warning_section(camps, update_str):
    """Section 2: Age-based warning thresholds."""
    # Thresholds by bucket_key
    warn_rules = [
        ('day1',   1.5, '📅 Day 1 — Under 1.5x ROAS'),
        ('day2',   1.5, '📅 Day 2 — Under 1.5x ROAS'),
        ('day3',   1.5, '📅 Day 3 — Under 1.5x ROAS'),
        ('day4_7', 2.0, '📆 Day 4–7 — Under 2.0x ROAS'),
        ('day7p',  2.0, '⏳ Day 7+ — Under 2.0x ROAS'),
    ]

    rows = []
    rows.append([f'⚠️  WARNING — Age-Based Thresholds  |  {update_str}', '', '', '', '', '', '', ''])
    rows.append(['Bucket', 'Campaign Name', 'Type', 'Budget (₹)', 'Spent (₹)', '% Spent',
                 'Today ROAS', 'Yday ROAS', '7D ROAS', '⚠️ Flag', 'Threshold'])

    for bk, thresh, label in warn_rules:
        matching = [c for c in camps if c['bucket_key'] == bk and c['roas'] < thresh]
        matching.sort(key=lambda x: -x['budget'])
        if not matching:
            continue
        bud   = sum(c['budget'] for c in matching)
        spnd  = sum(c['spend']  for c in matching)
        rows.append([f'{label}  |  {len(matching)} camps  |  ₹{bud:,.0f} budget  |  {pct(spnd,bud)} spent',
                     '', '', '', '', '', f'< {thresh}x', '', '', '', ''])
        for c in matching:
            flag = ''
            if c['above_yday']:
                flag = f"✅ Was {c['roas_yday']}x yday"
            elif c['max_roas'] >= thresh:
                flag = f"📈 Max {c['max_roas']}x"
            rows.append([
                c['bucket_label'],
                c['name'],
                c['camp_type'],
                int(c['budget']),
                int(c['spend']),
                pct(c['spend'], c['budget']),
                fmt_roas(c['roas']),
                fmt_roas(c['roas_yday']) if c['roas_yday'] is not None else '—',
                fmt_roas(c['roas_7d'])   if c['roas_7d']   is not None else '—',
                flag,
                f'< {thresh}x',
            ])

    rows.append([''])
    return rows


def build_budget_type_section(camps, update_str):
    """Section 3: Budget by Day Bucket × Campaign Type with collective + segment ROAS."""
    rows = []
    rows.append([f'📊  BUDGET BY DAY × TYPE  |  {update_str}', '', '', '', '', '', '', ''])

    bucket_order_list = ['day0','day1','day2','day3','day4_7','day7p']
    TYPES = ['Sales', 'Retarget', 'TOF']

    # Header
    header = ['Bucket', 'Total Budget (₹)', 'Total Spend (₹)', 'Overall ROAS',
              'Sales Bud (₹)', 'Sales ROAS',
              'Retarget Bud (₹)', 'Retarget ROAS',
              'TOF Bud (₹)', 'TOF ROAS']
    rows.append(header)

    # Grand totals
    grand = defaultdict(lambda: {'budget':0,'spend':0,'rev':0})

    def weighted_roas(camp_list):
        sp = sum(c['spend'] for c in camp_list)
        rv = sum(c['roas'] * c['spend'] for c in camp_list if c['roas'] > 0)
        return round(rv/sp, 2) if sp > 0 else '—'

    for bk in bucket_order_list:
        bc = [c for c in camps if c['bucket_key'] == bk]
        if not bc: continue
        label = bc[0]['bucket_label']

        total_bud  = sum(c['budget'] for c in bc)
        total_spnd = sum(c['spend']  for c in bc)
        total_roas = weighted_roas(bc)

        type_data = {}
        for t in TYPES:
            tc  = [c for c in bc if c['camp_type'] == t]
            bud = sum(c['budget'] for c in tc)
            type_data[t] = {'bud': bud, 'roas': weighted_roas(tc) if tc else '—'}

        rows.append([
            label,
            int(total_bud), int(total_spnd), total_roas,
            int(type_data['Sales']['bud']),   type_data['Sales']['roas'],
            int(type_data['Retarget']['bud']), type_data['Retarget']['roas'],
            int(type_data['TOF']['bud']),      type_data['TOF']['roas'],
        ])

        for t in TYPES:
            grand[t]['budget'] += type_data[t]['bud']

    # Grand total row
    all_bud  = sum(c['budget'] for c in camps)
    all_spnd = sum(c['spend']  for c in camps)
    all_roas = weighted_roas(camps)
    rows.append([
        'GRAND TOTAL',
        int(all_bud), int(all_spnd), all_roas,
        int(grand['Sales']['budget']),   '—',
        int(grand['Retarget']['budget']), '—',
        int(grand['TOF']['budget']),      '—',
    ])

    rows.append([''])
    return rows


# ── Write to sheet ────────────────────────────────────────────────────────────

def load_morning_snapshot(date_str):
    """Load the very first (10AM) snapshot of the day."""
    path = f'/tmp/closing_morning_{date_str}.json'
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {}

def save_morning_snapshot(date_str, camps, run_label):
    """Save 10AM snapshot — only written once per day."""
    path = f'/tmp/closing_morning_{date_str}.json'
    if not os.path.exists(path):
        data = {c['cid']: {
            'name':      c['name'],
            'roas':      c['roas'],
            'roas_yday': c.get('roas_yday'),
            'roas_7d':   c.get('roas_7d'),
            'budget':    c['budget'],
            'camp_type': c['camp_type'],
            'bucket_label': c['bucket_label'],
            'run_label': run_label,
        } for c in camps}
        with open(path, 'w') as f:
            json.dump(data, f)
        print(f"  💾 Morning snapshot saved ({len(data)} camps)")


def build_closed_section(current_camps, prev_snapshot, morning_snapshot, update_str):
    """
    Two parts:
    1. Closed since LAST RUN (prev snapshot)
    2. Cumulative closed SINCE MORNING (10AM snapshot) — full day view
    """
    current_cids = {c['cid'] for c in current_camps}
    rows = []

    # ── Part 1: Closed since last run ────────────────────────────────────────
    if prev_snapshot:
        closed_last = [info for cid, info in prev_snapshot.items() if cid not in current_cids]
        if not closed_last:
            rows.append([f'✅  CLOSED SINCE LAST RUN ({update_str}) — None', '', '', '', '', '', '', '', '', '', ''])
        else:
            closed_last.sort(key=lambda x: -x.get('budget', 0))
            rows.append([f'✅  CLOSED/PAUSED SINCE LAST RUN  |  {update_str}  |  {len(closed_last)} camps  |  Budget freed: ₹{sum(i.get("budget",0) for i in closed_last):,.0f}',
                         '', '', '', '', '', '', '', '', '', ''])
            rows.append(['Bucket', 'Campaign Name', 'Type', 'Was Budget (₹)', 'Was Today ROAS', 'Was Yday ROAS', 'Was 7D ROAS', 'Detected in Run', '', '', ''])
            for info in closed_last:
                rows.append([
                    info.get('bucket_label', '—'),
                    info.get('name', ''),
                    info.get('camp_type', '—'),
                    int(info.get('budget', 0)),
                    f"{info.get('roas','?')}x",
                    f"{info.get('roas_yday','—')}x" if info.get('roas_yday') is not None else '—',
                    f"{info.get('roas_7d','—')}x"   if info.get('roas_7d')   is not None else '—',
                    info.get('run_label', ''),
                    '', '', '',
                ])
        rows.append([''])

    # ── Part 2: Cumulative since morning ─────────────────────────────────────
    if not morning_snapshot:
        return rows

    closed_morning = [info for cid, info in morning_snapshot.items() if cid not in current_cids]

    if not closed_morning:
        rows.append([f'📋  TOTAL CLOSED SINCE 10 AM  |  {update_str} — None yet', '', '', '', '', '', '', '', '', '', ''])
        rows.append([''])
        return rows

    closed_morning.sort(key=lambda x: -x.get('budget', 0))
    total_bud = sum(i.get('budget', 0) for i in closed_morning)

    rows.append([f'📋  TOTAL CLOSED SINCE 10 AM  |  {update_str}  |  {len(closed_morning)} camps  |  Budget freed: ₹{total_bud:,.0f}',
                 '', '', '', '', '', '', '', '', '', ''])

    # Segmentation summary
    from collections import defaultdict
    by_type = defaultdict(lambda: {'count': 0, 'budget': 0})
    for info in closed_morning:
        t = info.get('camp_type', 'Other')
        by_type[t]['count']  += 1
        by_type[t]['budget'] += info.get('budget', 0)

    seg_summary = '   |   '.join(f"{t}: {d['count']} camps ₹{d['budget']:,.0f}" for t, d in sorted(by_type.items()))
    rows.append([f'Segmentation:  {seg_summary}', '', '', '', '', '', '', '', '', '', ''])
    rows.append(['Bucket', 'Campaign Name', 'Type', 'Was Budget (₹)', 'Was Today ROAS', 'Was Yday ROAS', 'Was 7D ROAS', 'First seen at', '', '', ''])

    for info in closed_morning:
        rows.append([
            info.get('bucket_label', '—'),
            info.get('name', ''),
            info.get('camp_type', '—'),
            int(info.get('budget', 0)),
            f"{info.get('roas','?')}x",
            f"{info.get('roas_yday','—')}x" if info.get('roas_yday') is not None else '—',
            f"{info.get('roas_7d','—')}x"   if info.get('roas_7d')   is not None else '—',
            info.get('run_label', '10:00 AM'),
            '', '', '',
        ])

    rows.append([''])
    return rows


def write_to_sheet(all_rows, tab_name, is_first_run=False):
    creds = Credentials.from_service_account_file(SA_FILE, scopes=SCOPES)
    gc    = gspread.authorize(creds)
    sh    = gc.open_by_key(SHEET_ID)

    existing_titles = [w.title for w in sh.worksheets()]

    if is_first_run or tab_name not in existing_titles:
        # First run of the day: delete + recreate clean
        if tab_name in existing_titles:
            sh.del_worksheet(sh.worksheet(tab_name))
            print(f"  Deleted old '{tab_name}' (first run refresh)")
        ws = sh.add_worksheet(title=tab_name, rows=max(len(all_rows) + 50, 400), cols=12)
        sh.reorder_worksheets([ws] + [w for w in sh.worksheets() if w.title != tab_name])
        start_row = 1
    else:
        # Subsequent runs: append below existing content
        ws = sh.worksheet(tab_name)
        existing_vals = ws.get_all_values()
        # Find last non-empty row
        last_row = len(existing_vals)
        while last_row > 0 and not any(v.strip() for v in existing_vals[last_row-1]):
            last_row -= 1
        start_row = last_row + 3  # 2 blank rows separator
        # Expand sheet if needed
        needed_rows = start_row + len(all_rows) + 20
        current_rows = ws.row_count
        if needed_rows > current_rows:
            ws.add_rows(needed_rows - current_rows + 100)
            print(f"  Expanded sheet to {needed_rows + 100} rows")
        print(f"  Appending to '{tab_name}' from row {start_row}")

    sheet_id = ws._properties['sheetId']

    # Pad all rows to same length
    max_cols = max(len(r) for r in all_rows) if all_rows else 10
    padded   = [(list(r) + [''] * max_cols)[:max_cols] for r in all_rows]

    # Write data
    ws.update(range_name=f'A{start_row}', values=padded, value_input_option='USER_ENTERED')

    # ── Minimal but effective formatting ──────────────────────────────────────
    def rgb(r, g, b):
        return {'red': r/255, 'green': g/255, 'blue': b/255}

    fmt_reqs = []

    def fmt(r1, c1, r2, c2, **props):
        fmt_reqs.append({'repeatCell': {
            'range': {'sheetId': sheet_id,
                      'startRowIndex': r1-1, 'endRowIndex': r2,
                      'startColumnIndex': c1-1, 'endColumnIndex': c2},
            'cell': {'userEnteredFormat': props},
            'fields': 'userEnteredFormat(' + ','.join(props.keys()) + ')'
        }})

    # Scan rows for section headers and apply colors
    SECTION_KEYWORDS = {
        '🚨': (rgb(139, 0, 0),    rgb(255, 255, 255)),
        '⚠️': (rgb(180, 90, 0),   rgb(255, 255, 255)),
        '📊': (rgb(30, 60, 120),  rgb(255, 255, 255)),
    }
    COL_HEADER_KEYWORDS = ['Age Bucket', 'Bucket', 'Camp', 'Budget', 'Spend', 'ROAS', 'Type', 'Total Budget', 'Yday ROAS', '7D ROAS']
    BUCKET_COLORS = {
        '🆕 Day 0':    rgb(142, 68, 173),
        '📅 Day 1':    rgb(41, 128, 185),
        '📅 Day 2':    rgb(52, 152, 219),
        '📅 Day 3':    rgb(26, 188, 156),
        '📆 Day 4–7':  rgb(243, 156, 18),
    }

    for i, row in enumerate(padded, 1):
        if not row or not row[0]: continue
        cell_val = str(row[0])

        # Section header
        is_section = False
        for emoji, (bg, fg) in SECTION_KEYWORDS.items():
            if cell_val.startswith(emoji):
                fmt(i, 1, i, max_cols,
                    backgroundColor=bg,
                    textFormat={'bold': True, 'fontSize': 11, 'foregroundColor': fg},
                    horizontalAlignment='LEFT')
                is_section = True
                break

        # Column headers
        if not is_section and any(kw in cell_val for kw in COL_HEADER_KEYWORDS):
            fmt(i, 1, i, max_cols,
                backgroundColor=rgb(52, 73, 94),
                textFormat={'bold': True, 'foregroundColor': rgb(255, 255, 255)},
                horizontalAlignment='CENTER')

        # Sub-header (⏳ / 📅 / 📆 lines with "camps")
        elif not is_section and any(emoji in cell_val for emoji in ['⏳','📅','📆','🆕']) and 'camps' in cell_val.lower():
            fmt(i, 1, i, max_cols,
                backgroundColor=rgb(40, 40, 40),
                textFormat={'bold': True, 'fontSize': 9, 'foregroundColor': rgb(255, 220, 100)},
                horizontalAlignment='LEFT')

        # TOTAL rows
        elif not is_section and cell_val in ('TOTAL', 'GRAND TOTAL', 'CLOSE TOTAL'):
            fmt(i, 1, i, max_cols,
                backgroundColor=rgb(30, 30, 30),
                textFormat={'bold': True, 'foregroundColor': rgb(255, 255, 255)},
                horizontalAlignment='RIGHT')

        # Data rows — bucket cell color
        elif not is_section and any(cell_val.startswith(b) for b in BUCKET_COLORS):
            for bname, bcolor in BUCKET_COLORS.items():
                if cell_val.startswith(bname.split(' ')[0] + ' ' + bname.split(' ')[1]):
                    fmt(i, 1, i, 1,
                        backgroundColor=bcolor,
                        textFormat={'bold': True, 'foregroundColor': rgb(255, 255, 255)},
                        horizontalAlignment='CENTER')
                    break
            # Alternate zebra for data rows
            row_bg = rgb(248, 248, 255) if i % 2 == 0 else rgb(255, 255, 255)
            fmt(i, 2, i, max_cols, backgroundColor=row_bg)

    # Apply all formatting in one batch
    if fmt_reqs:
        for chunk_start in range(0, len(fmt_reqs), 200):
            sh.batch_update({'requests': fmt_reqs[chunk_start:chunk_start+200]})

    # Column widths
    sh.batch_update({'requests': [
        {'updateDimensionProperties': {
            'range': {'sheetId': sheet_id, 'dimension': 'COLUMNS',
                      'startIndex': i, 'endIndex': i+1},
            'properties': {'pixelSize': w}, 'fields': 'pixelSize'
        }} for i, w in enumerate([130, 400, 80, 90, 90, 75, 80, 90, 80, 80, 80, 80])
    ]})

    print(f"  ✅ Written {len(padded)} rows to '{tab_name}'")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--date', default=datetime.now(IST).strftime('%Y-%m-%d'))
    args = parser.parse_args()

    now      = datetime.now(IST)
    date_str = args.date
    date_obj = datetime.strptime(date_str, '%Y-%m-%d')
    dl       = f"{date_obj.day} {date_obj.strftime('%b').upper()} {date_obj.strftime('%y')}"
    tab_name = f"🔴 Closing {dl}"
    run_hour   = now.hour
    update_str = now.strftime('%d %b %I:%M %p IST')
    # 10 AM run is the first run of the day — recreates tab fresh
    # Subsequent runs (12PM, 3PM, 6PM) append below
    is_first_run = (run_hour < 11)  # before 11 AM IST = first run

    print(f"\n📋 Closing Watchlist — {date_str} | {update_str} | {'FIRST RUN' if is_first_run else 'APPEND'}")

    # Load snapshots
    prev_snapshot    = load_snapshot(date_str)
    morning_snapshot = load_morning_snapshot(date_str)
    has_prev         = bool(prev_snapshot)

    # Fetch live data
    print("\n📡 Fetching live Meta data...")
    camps = fetch_sm_data(date_str)
    print(f"  Total active camps with spend: {len(camps)}")

    if not camps:
        print("  ⚠️  No data — check Meta token or no spend today")
        return

    # Save snapshots
    save_snapshot(date_str, camps, update_str)
    if is_first_run:
        save_morning_snapshot(date_str, camps, update_str)  # only saves once

    # Build all sections
    all_rows = []

    # Closed-camps section (only on non-first runs)
    if not is_first_run and has_prev:
        all_rows += build_closed_section(camps, prev_snapshot, morning_snapshot, update_str)

    all_rows += build_critical_section(camps, update_str)
    all_rows += build_warning_section(camps, update_str)
    all_rows += build_budget_type_section(camps, update_str)

    # Quick summary print
    critical_camps = [c for c in camps if c['roas'] < 1.0]
    total_bud  = sum(c['budget'] for c in critical_camps)
    zero_bud   = sum(c['budget'] for c in critical_camps if c['is_zero_roas'])
    total_spnd = sum(c['spend']  for c in critical_camps)
    closed_cnt = len([cid for cid in prev_snapshot if cid not in {c['cid'] for c in camps}]) if has_prev else 0
    print(f"\n  Critical under 1x: {len(critical_camps)} camps | ₹{total_bud:,.0f} budget | ₹{total_spnd:,.0f} spent | ₹{zero_bud:,.0f} zero ROAS")
    if closed_cnt: print(f"  Closed/paused since last run: {closed_cnt} camps")

    # Write to sheet
    print(f"\n📝 Writing to '{tab_name}'...")
    write_to_sheet(all_rows, tab_name, is_first_run=is_first_run)

    print(f"\n✅ Done — {update_str}")
    print(f"   🔗 https://docs.google.com/spreadsheets/d/{SHEET_ID}")


if __name__ == '__main__':
    main()
