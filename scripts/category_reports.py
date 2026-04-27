#!/usr/bin/env python3
"""
Category Heads Reports — one tab per category in the GHA sheet.

The ops team has internal category heads (Skin / Hair / Crystal Home Decor /
Crystal Accessory / 24K Jewellery / Perfumes / Aibot). Each tab is a dense
single-page report a head can pin and refresh on:

  Section A: 🏆 Best Creatives — top 10 ads in this category by ROAS today
             (with min spend gate). Columns: Ad Name | Type | CTR | CPM |
             CPV | Cost/Reach | Spend | ROAS
  Section B: 🚨 Worst Creatives — bottom 10 by ROAS, same columns
  Section C: 🔗 Landing Pages — collection URLs used in ads, with spend +
             clicks + LPVs + purchases + CVR (purchases / link clicks)
  Section D: 🆕 New Pushed Creatives — ads created in the last 7 days,
             with their performance so far
  Section E: 💰 Budget Summary — current daily budget on this category,
             day-wise spend over last 7 days, sales vs retarget split, ROAS

All sections pull live SM-portal data from Meta API + ad-creative metadata.

Usage:
  python3 scripts/category_reports.py                  # all categories, today
  python3 scripts/category_reports.py --date 2026-04-26
  python3 scripts/category_reports.py --category Skin  # rebuild one tab
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
sys.path.insert(0, str(_REPO_ROOT / 'scripts'))
from product_catalogue import derive_category_v2, derive_type

TOKEN    = os.getenv('META_ACCESS_TOKEN')
SA_FILE  = os.environ.get('GOOGLE_SERVICE_ACCOUNT_FILE') or str(_REPO_ROOT / 'google-service-account.json')
SHEET_ID = os.environ.get('REPORTS_SHEET_ID') or '1hJ3IS2VDtTAEyyJIV__jvts9CMQdYhyxKAfWKtrkUH4'
SCOPES   = ['https://www.googleapis.com/auth/spreadsheets']
GRAPH    = 'https://graph.facebook.com/v19.0'
IST      = ZoneInfo('Asia/Kolkata')

# SM portal — same scope as today_live_report
ACCOUNTS = [
    ('SM_FRAGRANCE_01',   'SM Fragrance 01'),
    ('SM_SKIN',           'SM Skin'),
    ('SM_HAIR',           'SM Hair'),
    ('SM_CRYSTALS',       'SM Crystals'),
    ('SM_PERFUME',        'SM Perfume'),
    ('SM_CREDIT_LINE_05', 'SM CL 05'),
    ('SM_CREDIT_LINE_06', 'SM CL 06'),
]

CATEGORIES = ['Skin', 'Hair', 'Crystal Home Decor', 'Crystal Accessory',
              '24K Jewellery', 'Perfumes', 'Aibot', 'Nutraceuticals']

MIN_SPEND_FOR_TOP   = 200    # ad needs >= ₹200 spend to qualify for "best/worst"
MIN_SPEND_FOR_PAGE  = 500    # landing page needs >= ₹500 to show in section C
NEW_CREATIVE_DAYS   = 7      # "new pushed" = created in last 7 days
BUDGET_HISTORY_DAYS = 7      # day-wise budget section spans last 7 days


# ── Helpers ──────────────────────────────────────────────────────────────────
def safe_float(x, default=0.0):
    try: return float(x or 0)
    except (TypeError, ValueError): return default

def safe_int(x, default=0):
    try: return int(float(x or 0))
    except (TypeError, ValueError): return default

def _action_sum(actions, types):
    if not actions: return 0
    return int(sum(safe_float(a.get('value', 0))
                   for a in actions if a.get('action_type') in types))

def extract_roas(raw):
    """purchase_roas → float, prefer 'value' (matches Meta UI)."""
    if not raw: return 0.0
    if isinstance(raw, list) and raw:
        item = raw[0]
        if isinstance(item, dict):
            return safe_float(item.get('value') or item.get('1d_click') or 0)
    return 0.0

def paginate(url, params, max_pages=20):
    rows, page = [], 0
    params = {**params, 'access_token': TOKEN}
    while page < max_pages:
        try:
            r = requests.get(url, params=params, timeout=30).json()
        except Exception as e:
            print(f"   request error: {e}", file=sys.stderr); return rows
        if 'error' in r:
            err = r['error']
            if err.get('code') == 17:
                time.sleep(60); continue
            print(f"   API: {err.get('message','')[:90]}", file=sys.stderr)
            return rows
        rows.extend(r.get('data', []))
        nxt = r.get('paging', {}).get('next')
        if not nxt: break
        url, params = nxt, {}
        page += 1
    return rows


# ── Fetch ─────────────────────────────────────────────────────────────────────
def fetch_ads_for_date(date_str):
    """Ad-level insights for date_str across all SM accounts."""
    out = []
    fields = ('ad_id,ad_name,campaign_id,campaign_name,adset_id,adset_name,'
              'spend,impressions,reach,clicks,inline_link_clicks,ctr,cpm,cpc,'
              'frequency,actions,action_values,purchase_roas,'
              'video_thruplay_watched_actions,video_p25_watched_actions,'
              'outbound_clicks')
    for key, acct_name in ACCOUNTS:
        acct = os.environ.get(key)
        if not acct: continue
        rows = paginate(f'{GRAPH}/{acct}/insights', {
            'level': 'ad',
            'fields': fields,
            'time_range': json.dumps({'since': date_str, 'until': date_str}),
            'filtering': json.dumps([{'field': 'spend', 'operator': 'GREATER_THAN', 'value': '0'}]),
            'limit': 500,
        })
        for r in rows:
            actions = r.get('actions') or []
            avals   = r.get('action_values') or []
            spend   = safe_float(r.get('spend'))
            impressions = safe_int(r.get('impressions'))
            reach   = safe_int(r.get('reach'))
            clicks  = safe_int(r.get('inline_link_clicks'))
            purchases = _action_sum(actions, {'omni_purchase', 'purchase'})
            revenue   = sum(safe_float(a.get('value')) for a in avals
                            if a.get('action_type') in {'omni_purchase', 'purchase'})
            out.append({
                'aid':         r.get('ad_id'),
                'ad_name':     r.get('ad_name', ''),
                'campaign_id': r.get('campaign_id'),
                'campaign':    r.get('campaign_name', ''),
                'adset_id':    r.get('adset_id'),
                'adset_name':  r.get('adset_name', ''),
                'account':     acct_name,
                'spend':       spend,
                'impressions': impressions,
                'reach':       reach,
                'clicks':      clicks,
                'ctr':         safe_float(r.get('ctr')),  # already %
                'cpm':         safe_float(r.get('cpm')),
                'cpc':         safe_float(r.get('cpc')),
                'frequency':   safe_float(r.get('frequency')),
                'roas':        extract_roas(r.get('purchase_roas')),
                'purchases':   purchases,
                'revenue':     revenue,
                'lpv':         _action_sum(actions, {'landing_page_view'}),
                'atc':         _action_sum(actions, {'omni_add_to_cart', 'add_to_cart'}),
                'outbound':    _action_sum(r.get('outbound_clicks'), {'outbound_click'}),
                'video_thruplay': _action_sum(r.get('video_thruplay_watched_actions'),
                                              {'video_view'}),
                'video_p25':   _action_sum(r.get('video_p25_watched_actions'),
                                            {'video_view'}),
                # derived per-row
                'cpr':         (spend / reach * 1000) if reach else 0,  # cost per 1k reach
                'cpv':         0,  # filled below if thruplay > 0
            })
            tp = out[-1]['video_thruplay']
            if tp > 0:
                out[-1]['cpv'] = spend / tp
            # Category from campaign name (ad name often has same product cues)
            out[-1]['category'] = derive_category_v2(out[-1]['campaign'] or out[-1]['ad_name'])
            out[-1]['type']     = derive_type(out[-1]['campaign'] or out[-1]['ad_name'])
    return out


def fetch_ad_creatives(ad_ids):
    """Bulk-fetch ad → creative.link mapping. Returns {ad_id: link_url or ''}."""
    out = {}
    if not ad_ids: return out
    # Meta supports ?ids=a,b,c for batch fetch (max ~50 per call)
    for i in range(0, len(ad_ids), 50):
        chunk = ad_ids[i:i+50]
        try:
            r = requests.get(f'{GRAPH}/', params={
                'ids': ','.join(chunk),
                'fields': 'id,creative{id,object_story_spec{link_data{link},video_data{call_to_action{value{link}}}}}',
                'access_token': TOKEN,
            }, timeout=30).json()
        except Exception as e:
            print(f"   creative fetch error: {e}", file=sys.stderr); continue
        if 'error' in r:
            print(f"   creative fetch API: {r['error'].get('message','')[:80]}", file=sys.stderr)
            continue
        # Response: {ad_id: {creative: {...}}, ...}
        for aid, body in r.items():
            link = ''
            cr = body.get('creative') or {}
            spec = cr.get('object_story_spec') or {}
            ld = spec.get('link_data') or {}
            link = ld.get('link', '') or ''
            if not link:
                vd = spec.get('video_data') or {}
                link = ((vd.get('call_to_action') or {}).get('value') or {}).get('link', '') or ''
            out[aid] = link
    return out


def fetch_recent_ads():
    """Returns set of ad_ids created in last NEW_CREATIVE_DAYS days, with created_time."""
    out = {}
    since_dt = datetime.now(IST) - timedelta(days=NEW_CREATIVE_DAYS)
    since_str = since_dt.strftime('%Y-%m-%dT%H:%M:%S+0530')
    for key, acct_name in ACCOUNTS:
        acct = os.environ.get(key)
        if not acct: continue
        rows = paginate(f'{GRAPH}/{acct}/ads', {
            'fields': 'id,name,created_time,effective_status',
            'filtering': json.dumps([
                {'field': 'created_time', 'operator': 'GREATER_THAN', 'value': since_str},
            ]),
            'limit': 500,
        })
        for ad in rows:
            out[ad['id']] = {
                'name': ad.get('name', ''),
                'created': ad.get('created_time', ''),
                'status': ad.get('effective_status', ''),
            }
    return out


def fetch_category_history(date_today, days=BUDGET_HISTORY_DAYS):
    """For each of the last N days, sum spend per category × type from Meta API.
    Returns {date: {category: {'sales': spend, 'retarget': spend, 'rev': revenue}}}
    """
    history = defaultdict(lambda: defaultdict(lambda: {'sales': 0.0, 'retarget': 0.0,
                                                        'rev_sales': 0.0, 'rev_retarget': 0.0}))
    end = datetime.strptime(date_today, '%Y-%m-%d').date()
    for offset in range(days - 1, -1, -1):
        day = end - timedelta(days=offset)
        ds = day.strftime('%Y-%m-%d')
        # Reuse fetch_ads_for_date — but only need spend + type/category, not full
        # detail. Tradeoff: ~10 API calls per day. Acceptable.
        ads = fetch_ads_for_date(ds)
        for a in ads:
            slot = history[ds][a['category']]
            if a['type'] == 'Retarget':
                slot['retarget'] += a['spend']
                slot['rev_retarget'] += a['revenue']
            else:
                slot['sales'] += a['spend']
                slot['rev_sales'] += a['revenue']
    return history


# ── Section builders ─────────────────────────────────────────────────────────
def section_best_worst(ads, ts_label):
    """Returns (best_rows, worst_rows) for the category."""
    eligible = [a for a in ads if a['spend'] >= MIN_SPEND_FOR_TOP]
    eligible.sort(key=lambda x: -x['roas'])
    return eligible[:10], list(reversed(eligible))[:10] if eligible else []


def section_landing_pages(ads, link_map):
    """Group ads by landing URL. Returns sorted list of dicts."""
    by_url = defaultdict(lambda: {'spend': 0.0, 'impressions': 0, 'clicks': 0,
                                   'lpv': 0, 'purchases': 0, 'revenue': 0.0,
                                   'ad_count': 0})
    for a in ads:
        url = link_map.get(a['aid'], '')
        if not url: continue
        slot = by_url[url]
        slot['spend']       += a['spend']
        slot['impressions'] += a['impressions']
        slot['clicks']      += a['clicks']
        slot['lpv']         += a['lpv']
        slot['purchases']   += a['purchases']
        slot['revenue']     += a['revenue']
        slot['ad_count']    += 1

    out = []
    for url, s in by_url.items():
        if s['spend'] < MIN_SPEND_FOR_PAGE: continue
        cvr = (s['purchases'] / s['clicks'] * 100) if s['clicks'] else 0
        roas = (s['revenue'] / s['spend']) if s['spend'] else 0
        out.append({'url': url, **s, 'cvr': cvr, 'roas': roas})
    out.sort(key=lambda x: -x['spend'])
    return out


def section_new_creatives(ads, recent_ad_ids):
    """Return ads in this category that were created in last N days, with perf."""
    new_ads = [a for a in ads if a['aid'] in recent_ad_ids]
    # Annotate with created_time
    for a in new_ads:
        meta = recent_ad_ids.get(a['aid'], {})
        a['_created'] = meta.get('created', '')[:10]
        a['_status']  = meta.get('status', '')
    new_ads.sort(key=lambda x: x.get('_created', ''), reverse=True)
    return new_ads


# ── Render ───────────────────────────────────────────────────────────────────
def fmt_money(n):
    return f'₹{int(n):,}' if n else '—'

def fmt_pct(p, dec=2):
    return f'{p:.{dec}f}%' if p else '—'

def fmt_roas(r):
    return f'{r:.2f}x' if r else '—'


def build_category_rows(category, ads, link_map, recent_ad_ids, history, ts_label):
    rows = []
    rows.append([f'📂  CATEGORY — {category.upper()}  |  Live Meta data  |  {ts_label}', '', '', '', '', '', '', '', ''])
    rows.append([f'   {len(ads)} ad(s) with spend today, total spend ₹{int(sum(a["spend"] for a in ads)):,}',
                 '', '', '', '', '', '', '', ''])
    rows.append([''])

    # ── Section A: Best ──────────────────────────────────────────────────────
    best, worst = section_best_worst(ads, ts_label)
    rows.append([f'🏆  TOP 10 CREATIVES BY ROAS  |  min spend ₹{MIN_SPEND_FOR_TOP}', '', '', '', '', '', '', '', ''])
    rows.append(['Rank', 'Ad Name', 'Type', 'CTR (%)', 'CPM (₹)', 'CPV (₹)', 'CPR/1K Reach (₹)', 'Spend (₹)', 'ROAS'])
    if best:
        for i, a in enumerate(best, 1):
            rows.append([
                i, a['ad_name'][:75], a['type'],
                fmt_pct(a['ctr']), fmt_money(a['cpm']),
                fmt_money(a['cpv']) if a['cpv'] else '—',
                fmt_money(a['cpr']),
                fmt_money(a['spend']), fmt_roas(a['roas']),
            ])
    else:
        rows.append(['—', 'No qualifying ads (need ≥ ₹200 spend)', '', '', '', '', '', '', ''])
    rows.append([''])

    # ── Section B: Worst ─────────────────────────────────────────────────────
    rows.append([f'🚨  BOTTOM 10 CREATIVES BY ROAS  |  min spend ₹{MIN_SPEND_FOR_TOP}', '', '', '', '', '', '', '', ''])
    rows.append(['Rank', 'Ad Name', 'Type', 'CTR (%)', 'CPM (₹)', 'CPV (₹)', 'CPR/1K Reach (₹)', 'Spend (₹)', 'ROAS'])
    if worst:
        for i, a in enumerate(worst, 1):
            rows.append([
                i, a['ad_name'][:75], a['type'],
                fmt_pct(a['ctr']), fmt_money(a['cpm']),
                fmt_money(a['cpv']) if a['cpv'] else '—',
                fmt_money(a['cpr']),
                fmt_money(a['spend']), fmt_roas(a['roas']),
            ])
    else:
        rows.append(['—', 'No qualifying ads', '', '', '', '', '', '', ''])
    rows.append([''])

    # ── Section C: Landing Pages ─────────────────────────────────────────────
    pages = section_landing_pages(ads, link_map)
    rows.append([f'🔗  LANDING PAGES IN USE  |  min spend ₹{MIN_SPEND_FOR_PAGE}  |  ad count grouped by URL',
                 '', '', '', '', '', '', '', ''])
    rows.append(['Rank', 'Collection / Page URL', '# Ads', 'Spend (₹)', 'Impressions', 'Link Clicks',
                 'LPVs', 'Purchases', 'CVR (% of clicks)'])
    if pages:
        for i, p in enumerate(pages, 1):
            rows.append([
                i, p['url'][:80], p['ad_count'], fmt_money(p['spend']),
                p['impressions'], p['clicks'], p['lpv'],
                p['purchases'], fmt_pct(p['cvr']),
            ])
    else:
        rows.append(['—', 'No qualifying landing pages today', '', '', '', '', '', '', ''])
    rows.append([''])

    # ── Section D: New Pushed Creatives ──────────────────────────────────────
    new_ads = section_new_creatives(ads, recent_ad_ids)
    rows.append([f'🆕  NEW CREATIVES PUSHED  |  created in last {NEW_CREATIVE_DAYS} days  |  performance so far',
                 '', '', '', '', '', '', '', ''])
    rows.append(['Created', 'Status', 'Ad Name', 'Type', 'Spend (₹)', 'CTR (%)', 'Purchases', 'Revenue (₹)', 'ROAS'])
    if new_ads:
        for a in new_ads:
            rows.append([
                a.get('_created', '—'), a.get('_status', '—'),
                a['ad_name'][:60], a['type'],
                fmt_money(a['spend']), fmt_pct(a['ctr']),
                a['purchases'], fmt_money(a['revenue']),
                fmt_roas(a['roas']),
            ])
    else:
        rows.append(['—', '—', f'No new ads in last {NEW_CREATIVE_DAYS} days for this category',
                     '', '', '', '', '', ''])
    rows.append([''])

    # ── Section E: Budget Summary ────────────────────────────────────────────
    rows.append([f'💰  BUDGET & SPEND SUMMARY  |  Last {BUDGET_HISTORY_DAYS} days  |  Sales vs Retarget',
                 '', '', '', '', '', '', '', ''])
    rows.append(['Date', 'Sales Spend (₹)', 'Sales Revenue (₹)', 'Sales ROAS',
                 'Retarget Spend (₹)', 'Retarget Revenue (₹)', 'Retarget ROAS',
                 'Total Spend (₹)', 'Overall ROAS'])
    history_for_cat = []
    for ds in sorted(history.keys()):
        slot = history[ds].get(category, {'sales': 0, 'retarget': 0, 'rev_sales': 0, 'rev_retarget': 0})
        s_spend, s_rev = slot['sales'], slot['rev_sales']
        r_spend, r_rev = slot['retarget'], slot['rev_retarget']
        total_spend = s_spend + r_spend
        total_rev   = s_rev + r_rev
        history_for_cat.append({
            'date': ds, 's_spend': s_spend, 's_rev': s_rev,
            'r_spend': r_spend, 'r_rev': r_rev,
            'total_spend': total_spend, 'total_rev': total_rev,
        })
        rows.append([
            ds,
            fmt_money(s_spend), fmt_money(s_rev),
            fmt_roas(s_rev / s_spend) if s_spend else '—',
            fmt_money(r_spend), fmt_money(r_rev),
            fmt_roas(r_rev / r_spend) if r_spend else '—',
            fmt_money(total_spend),
            fmt_roas(total_rev / total_spend) if total_spend else '—',
        ])
    # 7-day total row
    if history_for_cat:
        ts_total = sum(h['total_spend'] for h in history_for_cat)
        ts_rev   = sum(h['total_rev']   for h in history_for_cat)
        ts_s     = sum(h['s_spend']     for h in history_for_cat)
        ts_r     = sum(h['r_spend']     for h in history_for_cat)
        ts_sr    = sum(h['s_rev']       for h in history_for_cat)
        ts_rr    = sum(h['r_rev']       for h in history_for_cat)
        rows.append([
            f'{BUDGET_HISTORY_DAYS}-day TOTAL',
            fmt_money(ts_s), fmt_money(ts_sr),
            fmt_roas(ts_sr / ts_s) if ts_s else '—',
            fmt_money(ts_r), fmt_money(ts_rr),
            fmt_roas(ts_rr / ts_r) if ts_r else '—',
            fmt_money(ts_total),
            fmt_roas(ts_rev / ts_total) if ts_total else '—',
        ])
    rows.append([''])
    return rows


def write_to_sheet(category, rows, ts_label):
    creds = Credentials.from_service_account_file(SA_FILE, scopes=SCOPES)
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(SHEET_ID)

    tab_name = f'📂 Category — {category}'
    titles = [w.title for w in sh.worksheets()]
    if tab_name in titles:
        sh.del_worksheet(sh.worksheet(tab_name))
    ws = sh.add_worksheet(title=tab_name, rows=max(200, len(rows) + 30), cols=10)

    padded = [r + [''] * max(0, 9 - len(r)) for r in rows]
    ws.update(range_name='A1', values=padded, value_input_option='USER_ENTERED')

    # Section formatting
    sheet_id = ws.id
    def rgb(r, g, b): return {'red': r/255, 'green': g/255, 'blue': b/255}
    fmt_reqs = []

    SECTION_COLORS = {
        '📂': rgb(30, 60, 120),    # main header — navy
        '🏆': rgb(34, 139, 34),    # best — green
        '🚨': rgb(180, 30, 30),    # worst — red
        '🔗': rgb(120, 60, 130),   # landing pages — purple
        '🆕': rgb(220, 130, 30),   # new — orange
        '💰': rgb(60, 100, 80),    # budget — dark green
    }

    for i, row in enumerate(padded, 1):
        if not row or not row[0]: continue
        first = str(row[0])
        for emoji, bg in SECTION_COLORS.items():
            if first.startswith(emoji):
                fmt_reqs.append({'repeatCell': {
                    'range': {'sheetId': sheet_id, 'startRowIndex': i-1, 'endRowIndex': i,
                              'startColumnIndex': 0, 'endColumnIndex': 9},
                    'cell': {'userEnteredFormat': {
                        'backgroundColor': bg,
                        'textFormat': {'bold': True, 'fontSize': 11,
                                       'foregroundColor': rgb(255, 255, 255)},
                    }},
                    'fields': 'userEnteredFormat(backgroundColor,textFormat)',
                }})
                break
        # Column-header row (Rank | Ad Name | ...) — first cell is 'Rank' or 'Date' etc.
        if first in ('Rank', 'Date'):
            fmt_reqs.append({'repeatCell': {
                'range': {'sheetId': sheet_id, 'startRowIndex': i-1, 'endRowIndex': i,
                          'startColumnIndex': 0, 'endColumnIndex': 9},
                'cell': {'userEnteredFormat': {
                    'backgroundColor': rgb(60, 60, 60),
                    'textFormat': {'bold': True, 'foregroundColor': rgb(255, 255, 255)},
                }},
                'fields': 'userEnteredFormat(backgroundColor,textFormat)',
            }})

    # Column widths
    fmt_reqs.append({'updateDimensionProperties': {
        'range': {'sheetId': sheet_id, 'dimension': 'COLUMNS', 'startIndex': 1, 'endIndex': 2},
        'properties': {'pixelSize': 380}, 'fields': 'pixelSize',
    }})

    if fmt_reqs:
        for chunk in range(0, len(fmt_reqs), 200):
            sh.batch_update({'requests': fmt_reqs[chunk:chunk+200]})

    print(f"   ✅ Wrote {len(padded)} rows to '{tab_name}'")


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    p = argparse.ArgumentParser()
    p.add_argument('--date', default=datetime.now(IST).strftime('%Y-%m-%d'))
    p.add_argument('--category', help='Build just one (e.g. Skin, Hair, "Crystal Home Decor")')
    p.add_argument('--no-history', action='store_true', help='Skip 7-day budget history (faster)')
    args = p.parse_args()

    if not TOKEN:
        print("META_ACCESS_TOKEN not set"); sys.exit(2)

    now = datetime.now(IST)
    ts_label = now.strftime('%d %b %Y, %H:%M IST')

    print(f"\n📂 Category Reports — {args.date} — {ts_label}\n")

    print("→ Fetching today's ad-level data…")
    ads = fetch_ads_for_date(args.date)
    print(f"   {len(ads)} ads with spend today")

    print("→ Fetching ad creative landing URLs…")
    link_map = fetch_ad_creatives([a['aid'] for a in ads])
    print(f"   {sum(1 for v in link_map.values() if v)} ads with link_data")

    print(f"→ Fetching recently-created ads (last {NEW_CREATIVE_DAYS} days)…")
    recent_ad_ids = fetch_recent_ads()
    print(f"   {len(recent_ad_ids)} recent ads found")

    history = {}
    if not args.no_history:
        print(f"→ Fetching {BUDGET_HISTORY_DAYS}-day history per category × type…")
        history = fetch_category_history(args.date, days=BUDGET_HISTORY_DAYS)
        print(f"   {len(history)} day(s) loaded")

    target_cats = [args.category] if args.category else CATEGORIES
    for cat in target_cats:
        cat_ads = [a for a in ads if a['category'] == cat]
        print(f"\n→ Building tab for {cat} ({len(cat_ads)} ads)…")
        rows = build_category_rows(cat, cat_ads, link_map, recent_ad_ids, history, ts_label)
        write_to_sheet(cat, rows, ts_label)
        time.sleep(1)  # avoid sheet API rate limits

    print(f"\n🎉 Done — {len(target_cats)} category tab(s) refreshed")


if __name__ == '__main__':
    main()
