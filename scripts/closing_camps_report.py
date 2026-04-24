#!/usr/bin/env python3
"""
Closing Camps Report — 100% live from Meta API.
- Fetches ALL active campaigns with spend today from Meta API
- Gets live 1D ROAS (today) and 7D ROAS (last 7 days)
- Enriches with metadata (category, type, age) from tracker tabs
- Filters: 1D ROAS < 1.0
- Section 1: Full watchlist (oldest → newest)
- Section 2: Recommended to close (oldest first, cap ₹2L)

Usage:
  python3 closing_camps_report.py         # today
  python3 closing_camps_report.py --date 2026-04-23
"""

import os, json, argparse, requests, time
from datetime import datetime, timedelta
from collections import defaultdict
from pathlib import Path

import gspread
from google.oauth2.service_account import Credentials
from dotenv import load_dotenv

# ── Path setup (Phase 2: GitHub-Actions-friendly paths) ──────────────────────
_REPO_ROOT = Path(__file__).resolve().parent.parent
STATE_DIR  = Path(os.environ.get('META_REPORTS_STATE_DIR') or (_REPO_ROOT / 'state'))
OUT_DIR    = Path(os.environ.get('META_REPORTS_OUT_DIR')   or (_REPO_ROOT / 'out'))
STATE_DIR.mkdir(parents=True, exist_ok=True)
OUT_DIR.mkdir(parents=True, exist_ok=True)
load_dotenv(_REPO_ROOT / '.env')

TOKEN    = os.getenv('META_ACCESS_TOKEN')
SA_FILE  = os.environ.get('GOOGLE_SERVICE_ACCOUNT_FILE') or str(_REPO_ROOT / 'google-service-account.json')
SHEET_ID = '11IAPsJlil75aehYf5IzpSaTCLcAgPk9-57p6ZuPNNQM'
SCOPES   = ['https://www.googleapis.com/auth/spreadsheets']
GRAPH    = 'https://graph.facebook.com/v19.0'

# All accounts with portal label
ACCOUNTS = [
    (os.getenv('SM_FRAGRANCE_01'),  'SM'),
    (os.getenv('SM_SKIN'),          'SM'),
    (os.getenv('SM_HAIR'),          'SM'),
    (os.getenv('SM_CRYSTALS'),      'SM'),
    (os.getenv('SM_PERFUME'),       'SM'),
    (os.getenv('SM_CREDIT_LINE_05'),'SM'),
    (os.getenv('SM_CREDIT_LINE_06'),'SM'),
    (os.getenv('SML_SKIN'),         'SML'),
    (os.getenv('SML_HAIR'),         'SML'),
    (os.getenv('SML_CRYSTALS'),     'SML'),
    (os.getenv('SML_CL_06'),        'SML'),
    (os.getenv('SML_CL_07'),        'SML'),
    (os.getenv('NBP_SKIN'),         'NBP'),
    (os.getenv('NBP_HAIR_PERFUME'), 'NBP'),
    (os.getenv('NBP_CRYSTALS'),     'NBP'),
]

def safe_float(v):
    try: return float(str(v).replace(',','').strip())
    except: return 0.0

def paginate(endpoint, params, retries=3):
    params = dict(params)
    params['access_token'] = TOKEN
    results = []
    for attempt in range(retries):
        try:
            r = requests.get(f"{GRAPH}/{endpoint}", params=params, timeout=30)
            data = r.json()
            if 'error' in data:
                if data['error'].get('code') == 17:
                    print("  ⏳ Rate limit, waiting 60s..."); time.sleep(60); continue
                return results
            results.extend(data.get('data', []))
            while 'paging' in data and 'next' in data.get('paging', {}):
                r2 = requests.get(data['paging']['next'], timeout=30)
                data = r2.json()
                results.extend(data.get('data', []))
            return results
        except Exception as e:
            time.sleep(5)
    return results

def extract_roas(raw, window_key):
    """Extract ROAS for a specific window key (e.g. '1d_click', '7d_click')."""
    if not raw: return 0.0
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                try:
                    # Prefer specific window key, fallback to 'value'
                    v = item.get(window_key) or item.get('value', 0)
                    return round(float(v or 0), 2)
                except: continue
    return 0.0

def age_label(start_date_str, report_dt):
    """Calculate age from start_date string (YYYY-MM-DD or DD-Mon-YYYY)."""
    if not start_date_str: return 0, '❓ Unknown'
    try:
        # Try ISO format first (from API), then formatted
        for fmt in ['%Y-%m-%d', '%d-%b-%Y']:
            try:
                start = datetime.strptime(start_date_str[:10], fmt[:len(fmt)])
                break
            except: continue
        else:
            return 0, '❓ Unknown'
        days = (report_dt - start).days
        days = max(0, days)
        if days == 0:   return 0, '🆕 Day 0'
        if days == 1:   return 1, '📅 Day 1'
        if days == 2:   return 2, '📅 Day 2'
        if days == 3:   return 3, '📅 Day 3'
        if days <= 7:   return days, '📆 Day 4–7'
        return days, f'⏳ Day 7+ ({days}d)'
    except: return 0, '❓ Unknown'

def bucket_sort_key(label):
    for k,v in [('Day 0',0),('Day 1',1),('Day 2',2),('Day 3',3),('Day 4',4)]:
        if k in label: return v
    return 5

def rgb(r,g,b): return {'red':r/255,'green':g/255,'blue':b/255}
BUCKET_COLORS = {'🆕 Day 0':rgb(142,68,173),'📅 Day 1':rgb(41,128,185),
                 '📅 Day 2':rgb(52,152,219),'📅 Day 3':rgb(26,188,156),'📆 Day 4–7':rgb(243,156,18)}
PORTAL_COLORS = {'SM':rgb(41,128,185),'SML':rgb(39,174,96),'NBP':rgb(192,57,43)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--date', default=datetime.now().strftime('%Y-%m-%d'))
    args = parser.parse_args()

    report_dt = datetime.strptime(args.date, '%Y-%m-%d')
    date_str  = args.date
    dl        = f"{report_dt.day} {report_dt.strftime('%b').upper()} {report_dt.strftime('%y')}"
    tab_name  = f"📊 Reports {dl}"
    since_7d  = (report_dt - timedelta(days=6)).strftime('%Y-%m-%d')

    print(f"\n🚨 Closing Camps Report — {date_str} (100% LIVE)")

    # ── Step 1: Pull LIVE data from Meta API ──────────────────────────────────
    # For each account: get today's insights (spend + 1D ROAS) and 7D ROAS
    # One campaign_id = ONE row. No duplicates.

    live = {}  # cid → {portal, name, account_name, spend, roas_1d, roas_7d, start_time, budget}

    for account_id, portal in ACCOUNTS:
        if not account_id: continue

        # --- 1D insights (today only, 1d_click window) ---
        rows_1d = paginate(f"{account_id}/insights", {
            'level':  'campaign',
            'fields': 'campaign_id,campaign_name,account_name,spend,purchase_roas,date_start',
            'action_attribution_windows': json.dumps(['1d_click']),
            'time_range': json.dumps({'since': date_str, 'until': date_str}),
            'filtering': json.dumps([{'field':'spend','operator':'GREATER_THAN','value':'0'}]),
            'limit': 500,
        })

        # --- 7D insights (last 7 days, 7d_click window) ---
        rows_7d = paginate(f"{account_id}/insights", {
            'level':  'campaign',
            'fields': 'campaign_id,purchase_roas',
            'action_attribution_windows': json.dumps(['7d_click']),
            'time_range': json.dumps({'since': since_7d, 'until': date_str}),
            'filtering': json.dumps([{'field':'spend','operator':'GREATER_THAN','value':'0'}]),
            'limit': 500,
        })

        # Map 7D ROAS by campaign
        roas_7d_map = {}
        for r in rows_7d:
            cid = r.get('campaign_id','')
            roas_7d_map[cid] = extract_roas(r.get('purchase_roas'), '7d_click')

        for r in rows_1d:
            cid   = r.get('campaign_id','')
            if not cid: continue
            spend = safe_float(r.get('spend', 0))
            if spend == 0: continue
            r1d   = extract_roas(r.get('purchase_roas'), '1d_click')
            if r1d >= 1.0: continue  # only want < 1.0

            # Get start_time from campaign API (once per unique campaign)
            live[cid] = {
                'cid':          cid,
                'portal':       portal,
                'name':         r.get('campaign_name', ''),
                'account_name': r.get('account_name', ''),
                'spend':        spend,
                'roas_1d':      r1d,
                'roas_7d':      roas_7d_map.get(cid, 0.0),
                'start_time':   '',   # filled below
                'budget':       0,    # filled from tracker tab
                'category':     '',
                'type':         '',
            }

        if rows_1d:
            low_roas = sum(1 for r in rows_1d if extract_roas(r.get('purchase_roas'),'1d_click') < 1.0)
            print(f"  {portal} {account_id[-6:]}: {len(rows_1d)} camps with spend | {low_roas} with 1D ROAS < 1")

    print(f"\n  Unique low-ROAS campaigns (before enrichment): {len(live)}")

    # ── Step 2: Fetch start_time + budget from Meta campaigns API ─────────────
    # Batch by account — get all active campaign metadata
    cid_needs = set(live.keys())
    creds = Credentials.from_service_account_file(SA_FILE, scopes=SCOPES)
    gc    = gspread.authorize(creds)
    sh    = gc.open_by_key(SHEET_ID)

    # Tracker tabs: deduplicated metadata (take FIRST row per CID)
    tracker_meta = {}  # cid → {budget, category, type, start_date}
    existing_titles = [w.title for w in sh.worksheets()]

    for portal in ['SM','SML','NBP']:
        for dl_try in [dl, f"{(report_dt-timedelta(1)).day} {(report_dt-timedelta(1)).strftime('%b').upper()} {(report_dt-timedelta(1)).strftime('%y')}",
                       f"{(report_dt-timedelta(2)).day} {(report_dt-timedelta(2)).strftime('%b').upper()} {(report_dt-timedelta(2)).strftime('%y')}"]:
            tab = f"{portal} {dl_try}"
            if tab not in existing_titles: continue
            ws = sh.worksheet(tab)
            rows = ws.get_all_values()
            for row in rows[1:]:
                if not row[0] or not any(v.strip() for v in row): continue
                cid = row[19].strip() if len(row)>19 else ''
                if not cid or cid in tracker_meta: continue  # deduplicate — first row wins
                tracker_meta[cid] = {
                    'budget':   safe_float(row[11] if len(row)>11 else 0),
                    'category': row[25].strip() if len(row)>25 else '',
                    'type':     row[22].strip() if len(row)>22 else '',
                    'start':    row[4].strip()  if len(row)>4  else '',
                }
            print(f"  📋 {tab}: loaded (tracker meta now: {len(tracker_meta)} camps)")
            break  # found tab, stop trying dates for this portal

    # Enrich live camps with tracker metadata
    for cid in list(live.keys()):
        meta = tracker_meta.get(cid, {})
        live[cid]['budget']   = meta.get('budget', 0)
        live[cid]['category'] = meta.get('category', 'Unmapped')
        live[cid]['type']     = meta.get('type', '')
        live[cid]['start_time'] = meta.get('start', '')

    # For camps with no tracker metadata (new today), get start_time from API
    # Only for camps still missing start_time
    missing_start = [cid for cid, d in live.items() if not d['start_time']]
    if missing_start:
        # We don't know which account they belong to without lookup
        # Just mark them as Day 0 (started today — they're new)
        for cid in missing_start:
            live[cid]['start_time'] = date_str

    # ── Step 3: Calculate age + bucket ────────────────────────────────────────
    all_camps = []
    for cid, d in live.items():
        age_days, bucket = age_label(d['start_time'], report_dt)
        all_camps.append({**d, 'age_days': age_days, 'bucket': bucket})

    # Sort: oldest first, then lowest 1D ROAS
    all_camps.sort(key=lambda x: (-x['age_days'], x['roas_1d']))

    total_budget = sum(c['budget'] for c in all_camps)
    total_spend  = sum(c['spend']  for c in all_camps)
    n            = len(all_camps)

    by_bucket = defaultdict(lambda: {'count':0,'budget':0,'spend':0})
    for c in all_camps:
        by_bucket[c['bucket']]['count']  += 1
        by_bucket[c['bucket']]['budget'] += c['budget']
        by_bucket[c['bucket']]['spend']  += c['spend']

    print(f"\n  ✅ {n} camps | ₹{total_budget:,.0f} budget | ₹{total_spend:,.0f} live spend")
    for b in sorted(by_bucket, key=bucket_sort_key):
        d = by_bucket[b]
        print(f"    {b}: {d['count']} | budget ₹{d['budget']:,.0f} | spend ₹{d['spend']:,.0f}")

    # ── Step 4: Recommended to close (oldest first, cap ₹2L) ─────────────────
    recommended, running = [], 0
    for c in all_camps:
        if running + c['budget'] <= 200000:
            recommended.append(c)
            running += c['budget']
    rec_spend = sum(c['spend'] for c in recommended)

    # ── Step 5: Write to sheet ────────────────────────────────────────────────
    if tab_name in existing_titles:
        ws = sh.worksheet(tab_name)
        all_vals = ws.get_all_values()
        # Remove old closing section
        for i, row in enumerate(all_vals):
            if row and any(x in str(row[0]) for x in ['CLOSING WATCHLIST','CLOSING CAMPS','POTENTIAL CLOSING']):
                sid = ws._properties['sheetId']
                sh.batch_update({'requests':[{'deleteDimension':{'range':{
                    'sheetId':sid,'dimension':'ROWS',
                    'startIndex':max(0,i-2),'endIndex':len(all_vals)+10}}}]})
                all_vals = all_vals[:max(0,i-2)]
                print(f"  Cleared old closing section")
                break
        start_row = max(len([r for r in all_vals if any(v.strip() for v in r)]) + 3, 3)
    else:
        ws = sh.add_worksheet(title=tab_name, rows=600, cols=20)
        sh.reorder_worksheets([ws]+[w for w in sh.worksheets() if w.title!=tab_name])
        start_row = 1

    sheet_id = ws._properties['sheetId']
    NCOLS     = 10
    sheet_rows, fmt_reqs = [], []
    cur = start_row

    def add(vals):
        nonlocal cur
        padded = (list(vals) + ['']*NCOLS)[:NCOLS]
        sheet_rows.append(padded); r=cur; cur+=1; return r

    def fmt(r1,c1,r2,c2,**p):
        fmt_reqs.append({'repeatCell':{'range':{'sheetId':sheet_id,
            'startRowIndex':r1-1,'endRowIndex':r2,'startColumnIndex':c1-1,'endColumnIndex':c2},
            'cell':{'userEnteredFormat':p},'fields':'userEnteredFormat('+','.join(p.keys())+')'}})

    def merge(r1,c1,r2,c2):
        fmt_reqs.append({'mergeCells':{'range':{'sheetId':sheet_id,
            'startRowIndex':r1-1,'endRowIndex':r2,'startColumnIndex':c1-1,'endColumnIndex':c2},
            'mergeType':'MERGE_ALL'}})

    def write_camps_section(camps, title, title_bg, hdr_bg, is_rec=False):
        bud_s  = sum(c['budget'] for c in camps)
        spnd_s = sum(c['spend']  for c in camps)

        r = add([title])
        merge(r,1,r,NCOLS)
        fmt(r,1,r,NCOLS, backgroundColor=title_bg,
            textFormat={'bold':True,'fontSize':11,'foregroundColor':rgb(255,255,255)},
            horizontalAlignment='LEFT', padding={'top':8,'bottom':8,'left':12})

        if is_rec:
            note = '⬆️  Green 7D ROAS (≥1.5x) = historically good camp — pause first, don\'t close. Red 7D = close confidently.'
        else:
            parts = '  |  '.join(f"{b}: {by_bucket[b]['count']} camps / ₹{by_bucket[b]['budget']:,.0f}"
                                  for b in sorted(by_bucket, key=bucket_sort_key))
            note = f'By age:  {parts}'
        r2 = add([note])
        merge(r2,1,r2,NCOLS)
        fmt(r2,1,r2,NCOLS, backgroundColor=rgb(40,40,40),
            textFormat={'italic':True,'fontSize':8,'foregroundColor':rgb(220,220,160)},
            horizontalAlignment='LEFT', padding={'top':3,'bottom':3,'left':10})

        r = add(['Age Bucket','Portal','Campaign Name','Category','Type',
                 'Budget (₹)','Live Spend (₹)','Live 1D ROAS','Live 7D ROAS','Start Date'])
        fmt(r,1,r,NCOLS, backgroundColor=hdr_bg,
            textFormat={'bold':True,'foregroundColor':rgb(255,255,255)},
            horizontalAlignment='CENTER', padding={'top':5,'bottom':5})

        for i, c in enumerate(camps):
            r1d = c['roas_1d']
            r7d = c['roas_7d']
            row_bg = rgb(255,248,248) if i%2==0 else rgb(255,255,255)
            r = add([c['bucket'], c['portal'], c['name'],
                     c['category'] or '—', c['type'] or '—',
                     int(c['budget']) if c['budget'] else '—',
                     int(c['spend']),
                     r1d if r1d else '—',
                     r7d if r7d else '—',
                     c['start_time'] or '—'])

            fmt(r,1,r,NCOLS, backgroundColor=row_bg, textFormat={'fontSize':9}, verticalAlignment='MIDDLE')
            fmt(r,3,r,3, horizontalAlignment='LEFT')
            fmt(r,6,r,7, horizontalAlignment='RIGHT')

            bc = BUCKET_COLORS.get(c['bucket'], rgb(100,100,100))
            fmt(r,1,r,1, backgroundColor=bc,
                textFormat={'bold':True,'fontSize':9,'foregroundColor':rgb(255,255,255)},
                horizontalAlignment='CENTER')
            pc = PORTAL_COLORS.get(c['portal'], rgb(80,80,80))
            fmt(r,2,r,2, backgroundColor=pc,
                textFormat={'bold':True,'foregroundColor':rgb(255,255,255)},
                horizontalAlignment='CENTER')

            # 1D ROAS color
            if r1d == 0:   fmt(r,8,r,8, textFormat={'bold':True,'foregroundColor':rgb(192,57,43)}, horizontalAlignment='CENTER')
            elif r1d<0.5:  fmt(r,8,r,8, textFormat={'bold':True,'foregroundColor':rgb(230,126,34)}, horizontalAlignment='CENTER')
            else:          fmt(r,8,r,8, textFormat={'foregroundColor':rgb(180,140,0)}, horizontalAlignment='CENTER')

            # 7D ROAS — green if ≥ 1.5 (good historically), red if < 1
            if isinstance(r7d,float) and r7d >= 1.5:
                fmt(r,9,r,9, textFormat={'bold':True,'foregroundColor':rgb(39,174,96)}, horizontalAlignment='CENTER')
            elif isinstance(r7d,float) and r7d < 1.0 and r7d > 0:
                fmt(r,9,r,9, textFormat={'bold':True,'foregroundColor':rgb(192,57,43)}, horizontalAlignment='CENTER')
            else:
                fmt(r,9,r,9, horizontalAlignment='CENTER')

        label = 'CLOSE TOTAL' if is_rec else 'TOTAL'
        r = add([label,'',f'{len(camps)} campaigns','','',int(bud_s),int(spnd_s),'','',''])
        fmt(r,1,r,NCOLS, backgroundColor=title_bg,
            textFormat={'bold':True,'foregroundColor':rgb(255,255,255)}, horizontalAlignment='RIGHT')
        fmt(r,1,r,3, horizontalAlignment='LEFT')

    # --- Write both sections ---
    write_camps_section(all_camps,
        f'🚨  CLOSING WATCHLIST — {dl} (LIVE)  |  {n} unique camps  |  Budget: ₹{total_budget:,.0f}  |  Spend so far: ₹{total_spend:,.0f}  |  Filter: 1D ROAS < 1.0',
        rgb(139,0,0), rgb(52,73,94))

    add([''])  # gap

    write_camps_section(recommended,
        f'✂️  RECOMMENDED TO CLOSE — {dl}  |  {len(recommended)} camps  |  Budget freed: ₹{running:,.0f}  |  Priority: oldest first, cap ₹2,00,000',
        rgb(160,50,0), rgb(100,30,0), is_rec=True)

    # Write to sheet
    print(f"\n  Writing {len(sheet_rows)} rows to '{tab_name}' from row {start_row}...")
    ws.update(range_name=f'A{start_row}', values=sheet_rows, value_input_option='USER_ENTERED')
    if fmt_reqs:
        # Split into batches of 200 to avoid API limits
        for i in range(0, len(fmt_reqs), 200):
            sh.batch_update({'requests': fmt_reqs[i:i+200]})

    # Column widths
    sh.batch_update({'requests':[{'updateDimensionProperties':{
        'range':{'sheetId':sheet_id,'dimension':'COLUMNS','startIndex':i,'endIndex':i+1},
        'properties':{'pixelSize':w},'fields':'pixelSize'}}
        for i,w in enumerate([120,55,370,110,90,90,90,85,85,95])]})

    print(f"\n✅ Done!")
    print(f"   Watchlist: {n} unique camps | ₹{total_budget:,.0f} budget")
    print(f"   Recommended to close: {len(recommended)} camps | ₹{running:,.0f}")
    print(f"   🔗 https://docs.google.com/spreadsheets/d/{SHEET_ID}")

if __name__ == '__main__':
    main()
