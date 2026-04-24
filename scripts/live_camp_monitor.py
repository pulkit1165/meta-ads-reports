#!/usr/bin/env python3
"""
Live Camp Monitor — 3-hourly refresh + 1 PM closing report

Runs every 3 hours via cron:
  0 */3 * * * cd /workspace && python3 live_camp_monitor.py
  0 13 * * * cd /workspace && python3 live_camp_monitor.py --closing --notify

On-demand:
  python3 live_camp_monitor.py --summary   # 3-liner WA snapshot
  python3 live_camp_monitor.py --closing   # full closing report
  python3 live_camp_monitor.py --notify    # send to WhatsApp

Data fetched: 100% live from Meta API (today only, 1d_click window)
Sheet: 🔴 Live Monitor tab (always overwritten)
Snapshot cache: /tmp/live_monitor_snapshot.json (for ROAS delta)

Closing logic (₹2L daily cap):
  - Day 0 + Day 1-2 → 40% = ₹80K  | threshold ROAS < 1.0
  - Day 3-7         → 30% = ₹60K  | threshold ROAS < 1.0
  - Day 7+          → 30% = ₹60K  | threshold ROAS < 1.5
"""

import os, sys, json, argparse, requests, time
from datetime import datetime, timedelta, timezone
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
SNAPSHOT = '/tmp/live_monitor_snapshot.json'

WA_NUMBER = '+919517744959'  # Pulkit

# Budget allocation for closing (₹2L total)
CLOSE_TOTAL_CAP = 200000
CLOSE_ALLOC = {
    'early':  0.40,  # Day 0 + Day 1-2   (ROAS < 1.0)
    'mid':    0.30,  # Day 3-7            (ROAS < 1.0)
    'old':    0.30,  # Day 7+             (ROAS < 1.5)
}
CLOSE_THRESHOLD = {
    'early': 1.0,
    'mid':   1.0,
    'old':   1.5,
}

ACCOUNTS = [
    (os.getenv('SM_FRAGRANCE_01'),   'SM'),
    (os.getenv('SM_SKIN'),           'SM'),
    (os.getenv('SM_HAIR'),           'SM'),
    (os.getenv('SM_CRYSTALS'),       'SM'),
    (os.getenv('SM_PERFUME'),        'SM'),
    (os.getenv('SM_CREDIT_LINE_05'), 'SM'),
    (os.getenv('SM_CREDIT_LINE_06'), 'SM'),
    (os.getenv('SML_SKIN'),          'SML'),
    (os.getenv('SML_HAIR'),          'SML'),
    (os.getenv('SML_CRYSTALS'),      'SML'),
    (os.getenv('SML_CL_06'),         'SML'),
    (os.getenv('SML_CL_07'),         'SML'),
    (os.getenv('NBP_SKIN'),          'NBP'),
    (os.getenv('NBP_HAIR_PERFUME'),  'NBP'),
    (os.getenv('NBP_CRYSTALS'),      'NBP'),
]

# ── Helpers ───────────────────────────────────────────────────────────────────

def now_ist():
    return datetime.now(IST)

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
                code = data['error'].get('code')
                if code == 17:
                    print("  ⏳ Rate limit — waiting 60s..."); time.sleep(60); continue
                print(f"  ⚠️  API error: {data['error'].get('message')}")
                return results
            results.extend(data.get('data', []))
            while 'paging' in data and 'next' in data.get('paging', {}):
                r2 = requests.get(data['paging']['next'], timeout=30)
                data = r2.json()
                results.extend(data.get('data', []))
            return results
        except Exception as e:
            print(f"  ⚠️  Request error (attempt {attempt+1}): {e}")
            time.sleep(5)
    return results

def extract_roas(raw, key='1d_click'):
    if not raw: return 0.0
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                try:
                    v = item.get(key) or item.get('value', 0)
                    return round(float(v or 0), 2)
                except: continue
    return 0.0

def age_info(start_str, today):
    """Return (age_days, bucket_label, bucket_group)."""
    if not start_str: return 0, '🆕 Day 0', 'early'
    try:
        start = datetime.strptime(start_str[:10], '%Y-%m-%d')
        days = max(0, (today - start).days)
        if days == 0:   return 0, '🆕 Day 0',    'early'
        if days == 1:   return 1, '📅 Day 1',    'early'
        if days == 2:   return 2, '📅 Day 2',    'early'
        if days == 3:   return 3, '📅 Day 3',    'mid'
        if days <= 7:   return days, '📆 Day 4–7', 'mid'
        return days, f'⏳ Day 7+ ({days}d)', 'old'
    except: return 0, '🆕 Day 0', 'early'

def bucket_order(label):
    for k,v in [('Day 0',0),('Day 1',1),('Day 2',2),('Day 3',3),('Day 4',4)]:
        if k in label: return v
    return 5

def load_snapshot():
    try:
        if os.path.exists(SNAPSHOT):
            with open(SNAPSHOT) as f: return json.load(f)
    except: pass
    return {}

def save_snapshot(data):
    with open(SNAPSHOT, 'w') as f:
        json.dump(data, f)

def load_tracker_meta(sh, today_dl):
    """Load category/type/budget from today's or yesterday's tracker tab."""
    meta = {}  # cid → {category, type, budget, start}
    for portal in ['SM','SML','NBP']:
        for dl in [today_dl]:
            tab = f"{portal} {dl}"
            try:
                ws   = sh.worksheet(tab)
                rows = ws.get_all_values()
                for row in rows[1:]:
                    if not row[0]: continue
                    cid = row[19].strip() if len(row)>19 else ''
                    if not cid or cid in meta: continue
                    meta[cid] = {
                        'budget':   safe_float(row[11] if len(row)>11 else 0),
                        'category': row[25].strip() if len(row)>25 else '',
                        'type':     row[22].strip() if len(row)>22 else '',
                        'start':    row[4].strip()  if len(row)>4  else '',
                    }
                break
            except: continue
    return meta

# ── Fetch live data from Meta API ─────────────────────────────────────────────

def fetch_live_data(date_str):
    """Fetch all active camps with today's 1D ROAS + 7D ROAS. Returns {cid: {...}}."""
    since_7d = (datetime.strptime(date_str,'%Y-%m-%d') - timedelta(days=6)).strftime('%Y-%m-%d')
    live = {}  # cid → data

    for account_id, portal in ACCOUNTS:
        if not account_id: continue

        # 1D (today)
        rows_1d = paginate(f"{account_id}/insights", {
            'level': 'campaign',
            'fields': 'campaign_id,campaign_name,account_name,spend,purchase_roas',
            'action_attribution_windows': json.dumps(['1d_click']),
            'time_range': json.dumps({'since': date_str, 'until': date_str}),
            'filtering': json.dumps([{'field':'spend','operator':'GREATER_THAN','value':'0'}]),
            'limit': 500,
        })

        # 7D (max window)
        rows_7d = paginate(f"{account_id}/insights", {
            'level': 'campaign',
            'fields': 'campaign_id,purchase_roas',
            'action_attribution_windows': json.dumps(['7d_click']),
            'time_range': json.dumps({'since': since_7d, 'until': date_str}),
            'filtering': json.dumps([{'field':'spend','operator':'GREATER_THAN','value':'0'}]),
            'limit': 500,
        })
        roas_7d_map = {r.get('campaign_id',''): extract_roas(r.get('purchase_roas'),'7d_click') for r in rows_7d}

        # Campaign metadata (start_time, budget)
        camps_raw = paginate(f"{account_id}/campaigns", {
            'fields': 'id,name,start_time,status,daily_budget,lifetime_budget,adsets{daily_budget,lifetime_budget}',
            'effective_status': '["ACTIVE"]',
            'limit': 500,
        })
        camp_map = {c['id']: c for c in camps_raw}

        for r in rows_1d:
            cid   = r.get('campaign_id','')
            if not cid: continue
            spend = safe_float(r.get('spend', 0))
            if spend == 0: continue

            roas_1d = extract_roas(r.get('purchase_roas'), '1d_click')
            roas_7d = roas_7d_map.get(cid, 0.0)

            camp  = camp_map.get(cid, {})
            start = camp.get('start_time', '')[:10] if camp.get('start_time') else ''

            # Budget
            braw = camp.get('daily_budget','') or camp.get('lifetime_budget','')
            if not braw or braw == '0':
                adsets = camp.get('adsets', {}).get('data', [])
                total  = sum(float(a.get('daily_budget',0) or a.get('lifetime_budget',0) or 0) for a in adsets)
                braw   = str(int(total)) if total else '0'
            try: budget = round(float(braw)/100, 0)
            except: budget = 0

            live[cid] = {
                'cid':      cid,
                'portal':   portal,
                'name':     r.get('campaign_name', camp.get('name','')),
                'account':  r.get('account_name',''),
                'spend':    spend,
                'roas_1d':  roas_1d,
                'roas_7d':  roas_7d,
                'budget':   budget,
                'start':    start,
            }

        print(f"  {portal} {account_id[-6:]}: {len(rows_1d)} camps with spend")

    return live

# ── Build enriched camp list ──────────────────────────────────────────────────

def build_camps(live, tracker_meta, snapshot, today_dt):
    camps = []
    today = today_dt.date()
    for cid, d in live.items():
        meta    = tracker_meta.get(cid, {})
        prev    = snapshot.get(cid, {})
        age_days, bucket, group = age_info(d['start'] or meta.get('start',''), today)
        roas_prev  = prev.get('roas_1d', None)
        delta_roas = round(d['roas_1d'] - roas_prev, 2) if roas_prev is not None and d['roas_1d'] else None

        camps.append({
            **d,
            'category':   meta.get('category','') or '',
            'type':       meta.get('type','')     or '',
            'budget':     d['budget'] or meta.get('budget', 0),
            'age_days':   age_days,
            'bucket':     bucket,
            'group':      group,
            'roas_prev':  roas_prev,
            'delta_roas': delta_roas,
        })
    return camps

# ── Closing logic ─────────────────────────────────────────────────────────────

def build_closing_list(camps):
    """
    ₹2L total, split:
      Early (Day 0 + 1-2) → 40% = ₹80K, ROAS < 1.0
      Mid   (Day 3-7)     → 30% = ₹60K, ROAS < 1.0
      Old   (Day 7+)      → 30% = ₹60K, ROAS < 1.5
    Within each group: oldest first, then lowest ROAS.
    """
    alloc = {g: round(CLOSE_TOTAL_CAP * pct) for g, pct in CLOSE_ALLOC.items()}
    thresh = CLOSE_THRESHOLD
    used   = {'early': 0, 'mid': 0, 'old': 0}
    result = []

    # Sort within each group: oldest first, then lowest ROAS
    by_group = defaultdict(list)
    for c in camps:
        by_group[c['group']].append(c)

    for grp in ['early','mid','old']:
        group_camps = sorted(by_group[grp], key=lambda x: (-x['age_days'], x['roas_1d']))
        for c in group_camps:
            if c['roas_1d'] >= thresh[grp]: continue   # above threshold, skip
            if used[grp] + c['budget'] > alloc[grp]:    continue   # over budget for this group
            result.append({**c, 'close_reason': f"ROAS {c['roas_1d']:.2f} < {thresh[grp]} | {c['bucket']}"})
            used[grp] += c['budget']

    return result, used, alloc

# ── Sheet colors ──────────────────────────────────────────────────────────────

def rgb(r,g,b): return {'red':r/255,'green':g/255,'blue':b/255}
BUCKET_CLR = {'🆕 Day 0':rgb(142,68,173),'📅 Day 1':rgb(41,128,185),'📅 Day 2':rgb(52,152,219),
              '📅 Day 3':rgb(26,188,156),'📆 Day 4–7':rgb(243,156,18)}
PORTAL_CLR = {'SM':rgb(41,128,185),'SML':rgb(39,174,96),'NBP':rgb(192,57,43)}

# ── Write Live Monitor tab ────────────────────────────────────────────────────

def write_live_tab(sh, camps, closing, used, alloc, update_time_str):
    TAB = '🔴 Live Monitor'
    existing = [w.title for w in sh.worksheets()]
    if TAB in existing:
        ws = sh.worksheet(TAB)
        ws.clear()
    else:
        ws = sh.add_worksheet(title=TAB, rows=800, cols=14)
        # Move to front
        all_ws = sh.worksheets()
        sh.reorder_worksheets([ws] + [w for w in all_ws if w.title != TAB])
    sheet_id = ws._properties['sheetId']

    NCOLS = 12
    rows, fmts = [], []
    cur = 1

    def add(vals):
        nonlocal cur
        rows.append((list(vals) + ['']*NCOLS)[:NCOLS])
        r = cur; cur += 1; return r

    def fmt(r1,c1,r2,c2,**p):
        fmts.append({'repeatCell':{'range':{'sheetId':sheet_id,
            'startRowIndex':r1-1,'endRowIndex':r2,'startColumnIndex':c1-1,'endColumnIndex':c2},
            'cell':{'userEnteredFormat':p},'fields':'userEnteredFormat('+','.join(p.keys())+')'}})

    def merge(r1,c1,r2,c2):
        fmts.append({'mergeCells':{'range':{'sheetId':sheet_id,
            'startRowIndex':r1-1,'endRowIndex':r2,'startColumnIndex':c1-1,'endColumnIndex':c2},
            'mergeType':'MERGE_ALL'}})

    total_spend  = sum(c['spend'] for c in camps)
    total_budget = sum(c['budget'] for c in camps)
    avg_roas     = round(sum(c['roas_1d']*c['spend'] for c in camps if c['roas_1d']) /
                         max(sum(c['spend'] for c in camps if c['roas_1d']), 1), 2)
    n_camps      = len(camps)
    n_low        = sum(1 for c in camps if c['roas_1d'] < 1.0)

    # ── Main header ───────────────────────────────────────────────────────────
    r = add([f'🔴 LIVE CAMP MONITOR  |  Updated: {update_time_str}  |  {n_camps} camps running  |  Spend: ₹{total_spend:,.0f}  |  Avg 1D ROAS: {avg_roas}x  |  ⚠️ Below 1 ROAS: {n_low} camps'])
    merge(r,1,r,NCOLS)
    fmt(r,1,r,NCOLS, backgroundColor=rgb(20,20,40),
        textFormat={'bold':True,'fontSize':12,'foregroundColor':rgb(255,220,80)},
        horizontalAlignment='LEFT', padding={'top':8,'bottom':8,'left':12})

    # ── Section 1: Age Bucket Summary ─────────────────────────────────────────
    by_bucket = defaultdict(lambda: {'count':0,'budget':0,'spend':0,'roas_sum':0,'roas_cnt':0})
    for c in camps:
        b = c['bucket']
        by_bucket[b]['count']    += 1
        by_bucket[b]['budget']   += c['budget']
        by_bucket[b]['spend']    += c['spend']
        if c['roas_1d']:
            by_bucket[b]['roas_sum'] += c['roas_1d'] * c['spend']
            by_bucket[b]['roas_cnt'] += c['spend']

    add([''])
    r = add(['📊  AGE BUCKET SUMMARY'] + ['']*11)
    merge(r,1,r,NCOLS)
    fmt(r,1,r,NCOLS, backgroundColor=rgb(30,30,60),
        textFormat={'bold':True,'fontSize':11,'foregroundColor':rgb(255,255,255)},
        horizontalAlignment='LEFT', padding={'top':6,'bottom':6,'left':10})

    r = add(['Age Bucket','Camps','Budget (₹)','Live Spend (₹)','Avg 1D ROAS','Below Threshold','Budget at Risk (₹)','','','','',''])
    fmt(r,1,r,7, backgroundColor=rgb(52,73,94),
        textFormat={'bold':True,'foregroundColor':rgb(255,255,255)}, horizontalAlignment='CENTER')

    for b in sorted(by_bucket.keys(), key=bucket_order):
        d   = by_bucket[b]
        avg = round(d['roas_sum']/d['roas_cnt'], 2) if d['roas_cnt'] else 0
        grp = 'early' if any(x in b for x in ['Day 0','Day 1','Day 2']) else ('old' if '7+' in b else 'mid')
        thresh = CLOSE_THRESHOLD[grp]
        low    = [c for c in camps if c['bucket']==b and c['roas_1d'] < thresh]
        at_risk= sum(c['budget'] for c in low)

        r = add([b, d['count'], int(d['budget']), int(d['spend']),
                 avg, len(low), int(at_risk), '', '', '', '', ''])
        bc = BUCKET_CLR.get(b, rgb(100,100,100))
        fmt(r,1,r,1, backgroundColor=bc, textFormat={'bold':True,'foregroundColor':rgb(255,255,255)}, horizontalAlignment='CENTER')
        fmt(r,2,r,7, horizontalAlignment='RIGHT')
        if avg < 1.0:
            fmt(r,5,r,5, textFormat={'bold':True,'foregroundColor':rgb(192,57,43)})
        elif avg < 1.5:
            fmt(r,5,r,5, textFormat={'foregroundColor':rgb(243,156,18)})
        else:
            fmt(r,5,r,5, textFormat={'bold':True,'foregroundColor':rgb(39,174,96)})

    # ── Section 2: Full Camp List by Bucket ────────────────────────────────────
    add([''])
    r = add([f'📋  ALL CAMPS BY AGE — {n_camps} total | ₹{total_budget:,.0f} budget | ₹{total_spend:,.0f} spend'] + ['']*11)
    merge(r,1,r,NCOLS)
    fmt(r,1,r,NCOLS, backgroundColor=rgb(30,30,60),
        textFormat={'bold':True,'fontSize':11,'foregroundColor':rgb(255,255,255)},
        horizontalAlignment='LEFT', padding={'top':6,'bottom':6,'left':10})

    r = add(['Age','Portal','Campaign Name','Cat','Type','Budget','Spend','1D ROAS','Prev ROAS','Δ ROAS','7D ROAS','Max?'])
    fmt(r,1,r,NCOLS, backgroundColor=rgb(52,73,94),
        textFormat={'bold':True,'foregroundColor':rgb(255,255,255)}, horizontalAlignment='CENTER',
        padding={'top':4,'bottom':4})

    camps_sorted = sorted(camps, key=lambda x: (bucket_order(x['bucket']), -x['budget']))
    for i, c in enumerate(camps_sorted):
        r1d  = c['roas_1d']
        rprev= c['roas_prev']
        rdelta= c['delta_roas']
        r7d  = c['roas_7d']
        row_bg = rgb(248,248,255) if i%2==0 else rgb(255,255,255)

        delta_str = (f"▲{rdelta:.2f}" if rdelta and rdelta > 0 else
                     f"▼{abs(rdelta):.2f}" if rdelta and rdelta < 0 else '—') if rdelta is not None else 'NEW'

        is_7d_max = r7d > 0 and r1d < r7d * 0.7  # significantly below 7D = bad day

        r = add([c['bucket'], c['portal'], c['name'],
                 (c['category'] or '')[:15], (c['type'] or '')[:10],
                 int(c['budget']) if c['budget'] else '—',
                 int(c['spend']),
                 r1d if r1d else '—',
                 rprev if rprev else '—',
                 delta_str,
                 r7d if r7d else '—',
                 '⚠️' if is_7d_max else ''])

        fmt(r,1,r,NCOLS, backgroundColor=row_bg, textFormat={'fontSize':9}, verticalAlignment='MIDDLE')
        fmt(r,3,r,3, horizontalAlignment='LEFT')
        fmt(r,6,r,7, horizontalAlignment='RIGHT')

        bc = BUCKET_CLR.get(c['bucket'], rgb(100,100,100))
        fmt(r,1,r,1, backgroundColor=bc, textFormat={'bold':True,'fontSize':9,'foregroundColor':rgb(255,255,255)}, horizontalAlignment='CENTER')
        pc = PORTAL_CLR.get(c['portal'], rgb(80,80,80))
        fmt(r,2,r,2, backgroundColor=pc, textFormat={'bold':True,'foregroundColor':rgb(255,255,255)}, horizontalAlignment='CENTER')

        # 1D ROAS color
        if r1d == 0:    fmt(r,8,r,8, textFormat={'bold':True,'foregroundColor':rgb(150,150,150)}, horizontalAlignment='CENTER')
        elif r1d < 1.0: fmt(r,8,r,8, textFormat={'bold':True,'foregroundColor':rgb(192,57,43)}, horizontalAlignment='CENTER')
        elif r1d < 1.5: fmt(r,8,r,8, textFormat={'foregroundColor':rgb(180,100,0)}, horizontalAlignment='CENTER')
        else:           fmt(r,8,r,8, textFormat={'bold':True,'foregroundColor':rgb(39,174,96)}, horizontalAlignment='CENTER')

        # Delta color
        if delta_str.startswith('▲'):    fmt(r,10,r,10, textFormat={'foregroundColor':rgb(39,174,96)}, horizontalAlignment='CENTER')
        elif delta_str.startswith('▼'):  fmt(r,10,r,10, textFormat={'foregroundColor':rgb(192,57,43)}, horizontalAlignment='CENTER')

    # ── Section 3: Closing Recommendations ────────────────────────────────────
    add([''])
    total_close_budget = sum(c['budget'] for c in closing)
    total_close_spend  = sum(c['spend']  for c in closing)

    r = add([f'✂️  RECOMMENDED TO CLOSE  |  {len(closing)} camps  |  ₹{total_close_budget:,.0f} freed  |  Oldest first within each group  |  Total cap ₹2,00,000'] + ['']*11)
    merge(r,1,r,NCOLS)
    fmt(r,1,r,NCOLS, backgroundColor=rgb(139,0,0),
        textFormat={'bold':True,'fontSize':11,'foregroundColor':rgb(255,255,255)},
        horizontalAlignment='LEFT', padding={'top':7,'bottom':7,'left':10})

    # Budget breakdown by group
    grp_labels = {'early':'Day 0+1+2 (40%)','mid':'Day 3–7 (30%)','old':'Day 7+ (30%)'}
    summary_parts = ' | '.join(f"{grp_labels[g]}: {sum(1 for c in closing if c['group']==g)} camps ₹{used[g]:,.0f}/₹{alloc[g]:,.0f}"
                                for g in ['early','mid','old'])
    r = add([summary_parts] + ['']*11)
    merge(r,1,r,NCOLS)
    fmt(r,1,r,NCOLS, backgroundColor=rgb(60,20,20),
        textFormat={'italic':True,'fontSize':8,'foregroundColor':rgb(220,180,180)},
        horizontalAlignment='LEFT', padding={'top':3,'bottom':3,'left':10})

    r = add(['Age','Portal','Campaign Name','Cat','Type','Budget (₹)','Spend (₹)','1D ROAS','7D ROAS','Threshold','Close Reason',''])
    fmt(r,1,r,11, backgroundColor=rgb(100,20,20),
        textFormat={'bold':True,'foregroundColor':rgb(255,255,255)}, horizontalAlignment='CENTER')

    for i, c in enumerate(sorted(closing, key=lambda x: (-x['age_days'], x['roas_1d']))):
        r1d = c['roas_1d']
        r7d = c['roas_7d']
        row_bg = rgb(255,240,240) if i%2==0 else rgb(255,248,248)

        r = add([c['bucket'], c['portal'], c['name'],
                 (c['category'] or '')[:15], (c['type'] or '')[:10],
                 int(c['budget']), int(c['spend']),
                 r1d if r1d else '—', r7d if r7d else '—',
                 CLOSE_THRESHOLD[c['group']], c['close_reason'], ''])

        fmt(r,1,r,NCOLS, backgroundColor=row_bg, textFormat={'fontSize':9}, verticalAlignment='MIDDLE')
        fmt(r,3,r,3, horizontalAlignment='LEFT')
        fmt(r,6,r,7, horizontalAlignment='RIGHT')
        bc = BUCKET_CLR.get(c['bucket'], rgb(100,100,100))
        fmt(r,1,r,1, backgroundColor=bc, textFormat={'bold':True,'fontSize':9,'foregroundColor':rgb(255,255,255)}, horizontalAlignment='CENTER')
        pc = PORTAL_CLR.get(c['portal'], rgb(80,80,80))
        fmt(r,2,r,2, backgroundColor=pc, textFormat={'bold':True,'foregroundColor':rgb(255,255,255)}, horizontalAlignment='CENTER')
        fmt(r,8,r,8, textFormat={'bold':True,'foregroundColor':rgb(192,57,43)}, horizontalAlignment='CENTER')
        # 7D ROAS — green if > 1.5
        if isinstance(r7d, float) and r7d >= 1.5:
            fmt(r,9,r,9, textFormat={'bold':True,'foregroundColor':rgb(39,174,96)}, horizontalAlignment='CENTER')

    r = add(['TOTAL','','',f'{len(closing)} camps to close','',int(total_close_budget),int(total_close_spend),'','','','',''])
    fmt(r,1,r,NCOLS, backgroundColor=rgb(139,0,0), textFormat={'bold':True,'foregroundColor':rgb(255,255,255)}, horizontalAlignment='RIGHT')
    fmt(r,1,r,4, horizontalAlignment='LEFT')

    # Write + format
    ws.update(range_name='A1', values=rows, value_input_option='USER_ENTERED')
    for i in range(0, len(fmts), 200):
        sh.batch_update({'requests': fmts[i:i+200]})

    # Column widths
    sh.batch_update({'requests':[{'updateDimensionProperties':{
        'range':{'sheetId':sheet_id,'dimension':'COLUMNS','startIndex':i,'endIndex':i+1},
        'properties':{'pixelSize':w},'fields':'pixelSize'}}
        for i,w in enumerate([115,55,360,100,85,90,90,80,80,80,80,40])]})

    print(f"  ✅ Live Monitor tab written ({len(rows)} rows)")

# ── Build WhatsApp summary ─────────────────────────────────────────────────────

def build_wa_summary(camps, closing, update_time_str, is_closing=False):
    total_spend  = sum(c['spend'] for c in camps)
    avg_roas     = round(sum(c['roas_1d']*c['spend'] for c in camps if c['roas_1d']) /
                         max(sum(c['spend'] for c in camps if c['roas_1d']), 1), 2)
    n_low        = sum(1 for c in camps if c['roas_1d'] < 1.0)
    close_budget = sum(c['budget'] for c in closing)
    n_close      = len(closing)

    if is_closing:
        # Full closing report at 1 PM
        by_group = defaultdict(list)
        for c in closing:
            by_group[c['group']].append(c)

        lines = [
            f"✂️ *Closing Report — {update_time_str}*",
            f"Total running: {len(camps)} camps | Spend: ₹{total_spend:,.0f} | Avg ROAS: {avg_roas}x",
            f"⚠️ Below threshold: {n_low} camps | Close recommendation: {n_close} camps ₹{close_budget:,.0f}",
            "",
            f"*Day 0+1+2 (40% = ₹{round(CLOSE_TOTAL_CAP*0.4):,.0f}):*",
        ]
        for c in sorted(by_group['early'], key=lambda x: (-x['age_days'], x['roas_1d']))[:5]:
            lines.append(f"  • {c['name'][:45]} | {c['bucket']} | 1D={c['roas_1d']} | ₹{int(c['budget']):,}")

        lines += ["", f"*Day 3–7 (30% = ₹{round(CLOSE_TOTAL_CAP*0.3):,.0f}):*"]
        for c in sorted(by_group['mid'], key=lambda x: (-x['age_days'], x['roas_1d']))[:5]:
            lines.append(f"  • {c['name'][:45]} | {c['bucket']} | 1D={c['roas_1d']} | ₹{int(c['budget']):,}")

        lines += ["", f"*Day 7+ (30% = ₹{round(CLOSE_TOTAL_CAP*0.3):,.0f}):*"]
        for c in sorted(by_group['old'], key=lambda x: (-x['age_days'], x['roas_1d']))[:5]:
            lines.append(f"  • {c['name'][:45]} | {c['bucket']} | 1D={c['roas_1d']} | 7D={c['roas_7d']} | ₹{int(c['budget']):,}")

        lines += ["", f"🔗 https://docs.google.com/spreadsheets/d/{SHEET_ID}"]
    else:
        # 3-liner snapshot
        lines = [
            f"📊 *Live {update_time_str}*",
            f"Running: {len(camps)} camps | ₹{total_spend:,.0f} spend | Avg 1D ROAS: {avg_roas}x",
            f"🔴 Below 1 ROAS: {n_low} camps | Rec. close: {n_close} camps ₹{close_budget:,.0f} | Sheet: docs.google.com/spreadsheets/d/{SHEET_ID[:20]}...",
        ]

    return '\n'.join(lines)

# ── Send WhatsApp ─────────────────────────────────────────────────────────────

def send_wa(message):
    """Send WA via OpenClaw WA bridge."""
    try:
        import subprocess
        result = subprocess.run(
            ['openclaw', 'send', WA_NUMBER, message],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            print("  ✅ WhatsApp sent")
        else:
            print(f"  ⚠️  WA send failed: {result.stderr[:100]}")
    except Exception as e:
        print(f"  ⚠️  WA send error: {e}")
        # Fallback: write to file for manual send
        with open('/tmp/pending_wa_message.txt', 'w') as f:
            f.write(message)
        print("  📝 Message saved to /tmp/pending_wa_message.txt")

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--date',    default=datetime.now(IST).strftime('%Y-%m-%d'))
    parser.add_argument('--closing', action='store_true', help='Include closing report')
    parser.add_argument('--notify',  action='store_true', help='Send WA notification')
    parser.add_argument('--summary', action='store_true', help='Print 3-liner summary only')
    args = parser.parse_args()

    now = datetime.now(IST)
    date_str     = args.date
    today_dt     = datetime.strptime(date_str, '%Y-%m-%d')
    update_str   = now.strftime('%d %b %I:%M %p IST')
    today_dl     = f"{today_dt.day} {today_dt.strftime('%b').upper()} {today_dt.strftime('%y')}"

    print(f"\n🔴 Live Camp Monitor — {date_str} | {update_str}")

    # Load previous snapshot
    snapshot = load_snapshot()

    # Fetch live data
    print("\n📡 Fetching live data from Meta API...")
    live = fetch_live_data(date_str)
    print(f"  Total camps with spend: {len(live)}")

    # Enrich from tracker tab
    creds = Credentials.from_service_account_file(SA_FILE, scopes=SCOPES)
    gc    = gspread.authorize(creds)
    sh    = gc.open_by_key(SHEET_ID)
    tracker_meta = load_tracker_meta(sh, today_dl)
    print(f"  Tracker meta: {len(tracker_meta)} camps loaded")

    # Build camps list
    camps = build_camps(live, tracker_meta, snapshot, today_dt)

    # Save new snapshot
    new_snapshot = {c['cid']: {'roas_1d': c['roas_1d'], 'spend': c['spend'], 'ts': now.isoformat()} for c in camps}
    save_snapshot(new_snapshot)

    # Build closing list
    closing, used, alloc = build_closing_list(camps)
    close_budget = sum(c['budget'] for c in closing)

    print(f"\n📊 Summary:")
    print(f"  Camps: {len(camps)} | Spend: ₹{sum(c['spend'] for c in camps):,.0f}")
    print(f"  Closing: {len(closing)} camps | ₹{close_budget:,.0f} budget")

    if args.summary:
        msg = build_wa_summary(camps, closing, update_str, is_closing=False)
        print(f"\n3-LINER:\n{msg}")
        if args.notify:
            send_wa(msg)
        return

    # Write to sheet
    print("\n📝 Writing Live Monitor tab...")
    write_live_tab(sh, camps, closing, used, alloc, update_str)

    # WhatsApp message
    msg = build_wa_summary(camps, closing, update_str, is_closing=args.closing)

    if args.closing or args.notify:
        print(f"\n📱 WhatsApp message:\n{msg}")
        if args.notify:
            send_wa(msg)
    else:
        print(f"\n3-liner (use --notify to send):\n{msg}")

    print(f"\n✅ Done — {update_str}")
    print(f"   🔗 https://docs.google.com/spreadsheets/d/{SHEET_ID}")

if __name__ == '__main__':
    main()
