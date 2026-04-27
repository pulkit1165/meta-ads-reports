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
def compute_category_summary(ads):
    """Returns spend-weighted aggregate metrics for a category's ads today.
    All ratios (CTR / CPM / CPV / CPR / ROAS) are properly weighted by spend
    or impressions/reach — NOT a flat average of per-ad ratios.
    """
    if not ads:
        return {
            'active_ads': 0, 'spend': 0.0, 'budget': 0.0, 'impressions': 0, 'reach': 0,
            'clicks': 0, 'thruplay': 0, 'purchases': 0, 'revenue': 0.0, 'lpv': 0,
            'ctr': 0.0, 'cpm': 0.0, 'cpv': 0.0, 'cpr_1k': 0.0, 'cpc': 0.0,
            'roas': 0.0, 'cvr': 0.0,
        }
    total_spend       = sum(a['spend']         for a in ads)
    total_impressions = sum(a['impressions']   for a in ads)
    total_reach       = sum(a['reach']         for a in ads)
    total_clicks      = sum(a['clicks']        for a in ads)
    total_thruplay    = sum(a['video_thruplay'] for a in ads)
    total_purchases   = sum(a['purchases']     for a in ads)
    total_revenue     = sum(a['revenue']       for a in ads)
    total_lpv         = sum(a['lpv']           for a in ads)

    return {
        'active_ads':  len(ads),
        'spend':       total_spend,
        'impressions': total_impressions,
        'reach':       total_reach,
        'clicks':      total_clicks,
        'thruplay':    total_thruplay,
        'purchases':   total_purchases,
        'revenue':     total_revenue,
        'lpv':         total_lpv,
        # Properly-weighted ratios:
        'ctr':    (total_clicks    / total_impressions * 100) if total_impressions else 0.0,
        'cpm':    (total_spend     / total_impressions * 1000) if total_impressions else 0.0,
        'cpv':    (total_spend     / total_thruplay)            if total_thruplay   else 0.0,
        'cpr_1k': (total_spend     / total_reach * 1000)        if total_reach     else 0.0,
        'cpc':    (total_spend     / total_clicks)              if total_clicks    else 0.0,
        'roas':   (total_revenue   / total_spend)               if total_spend     else 0.0,
        'cvr':    (total_purchases / total_clicks * 100)        if total_clicks    else 0.0,
    }


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

    # ── Summary Block (snapshot: spend-weighted CTR/CPM/CPV/CPR + totals) ────
    s = compute_category_summary(ads)
    rows.append([f'📊  SUMMARY  |  {category}  |  Spend-weighted aggregates', '', '', '', '', '', '', '', ''])
    rows.append(['Metric', 'Value', 'Notes', '', '', '', '', '', ''])
    rows.append(['Active Ads',     s['active_ads'],            'with spend today', '', '', '', '', '', ''])
    rows.append(['Spend Today',    f"₹{int(s['spend']):,}",   '', '', '', '', '', '', ''])
    rows.append(['Revenue',        f"₹{int(s['revenue']):,}", f"{s['purchases']:,} purchases", '', '', '', '', '', ''])
    rows.append(['ROAS',           fmt_roas(s['roas']),        'spend-weighted', '', '', '', '', '', ''])
    rows.append(['CTR',            fmt_pct(s['ctr']),          f"{s['clicks']:,} link clicks / {s['impressions']:,} imp", '', '', '', '', '', ''])
    rows.append(['CPM',            f"₹{int(s['cpm']):,}",     'cost per 1k impressions', '', '', '', '', '', ''])
    rows.append(['CPV',            (f"₹{s['cpv']:.2f}") if s['cpv'] else '—', f"{s['thruplay']:,} thruplays", '', '', '', '', '', ''])
    rows.append(['CPR / 1k Reach', f"₹{int(s['cpr_1k']):,}", f"{s['reach']:,} unique reach", '', '', '', '', '', ''])
    rows.append(['CPC',            f"₹{s['cpc']:.2f}" if s['cpc'] else '—', '', '', '', '', '', '', ''])
    rows.append(['CVR (% of clicks)', fmt_pct(s['cvr']), '', '', '', '', '', '', ''])
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
        '📊': rgb(70, 90, 140),    # summary — slate blue
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
        # Column-header row (Rank | Ad Name | ...) — first cell is 'Rank' or 'Date' or 'Metric' etc.
        if first in ('Rank', 'Date', 'Metric', 'Created'):
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


# ── HTML dashboard render ─────────────────────────────────────────────────────
def render_categories_dashboard_html(per_cat_ads, link_map, recent_ad_ids, history,
                                     ts_label, date_str, sheet_url):
    """Single-page HTML dashboard with a tab strip for each category and the
    same 5 sections we write to the sheet. Output goes to out/categories.html
    and gets deployed to Cloudflare Pages alongside the NTN dashboard.
    `per_cat_ads`: {category: [ad_dict, ...]}
    """
    OUT_DIR = Path(os.environ.get('META_REPORTS_OUT_DIR') or (_REPO_ROOT / 'out'))
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    def cls(roas):
        if roas >= 2.0: return 'rg'
        if roas >= 1.5: return 'ro'
        return 'rr'

    def render_section_best_worst(ads, label, emoji_class):
        """Returns inner HTML for the Top/Bottom 10 section."""
        eligible = [a for a in ads if a['spend'] >= MIN_SPEND_FOR_TOP]
        if not eligible:
            return f'<p class="empty">No qualifying ads (need ≥ ₹{MIN_SPEND_FOR_TOP} spend today)</p>'
        eligible.sort(key=lambda x: -x['roas'])
        rows = eligible[:10] if label == 'best' else list(reversed(eligible))[:10]
        body = ''
        for i, a in enumerate(rows, 1):
            body += (
                f"<tr><td><b>{i}</b></td>"
                f"<td style='font-size:11px'>{a['ad_name'][:75]}</td>"
                f"<td>{a['type']}</td>"
                f"<td>{a['ctr']:.2f}%</td>"
                f"<td>₹{int(a['cpm']):,}</td>"
                f"<td>{('₹' + str(round(a['cpv'], 2))) if a['cpv'] else '—'}</td>"
                f"<td>₹{int(a['cpr']):,}</td>"
                f"<td>₹{int(a['spend']):,}</td>"
                f"<td class='{cls(a['roas'])}'>{a['roas']:.2f}x</td></tr>"
            )
        return ('<table>'
                '<thead><tr><th>#</th><th>Ad Name</th><th>Type</th><th>CTR</th>'
                '<th>CPM</th><th>CPV</th><th>CPR/1k</th><th>Spend</th><th>ROAS</th></tr></thead>'
                f'<tbody>{body}</tbody></table>')

    def render_landing_pages(ads):
        pages = section_landing_pages(ads, link_map)
        if not pages:
            return '<p class="empty">No landing pages with ≥ ₹500 spend today</p>'
        body = ''
        for i, p in enumerate(pages, 1):
            short = p['url'].replace('https://', '').replace('http://', '')
            short = short[:90] + ('…' if len(short) > 90 else '')
            body += (
                f"<tr><td><b>{i}</b></td>"
                f"<td style='font-size:11px'><a href='{p['url']}' target='_blank' rel='noopener'>{short}</a></td>"
                f"<td>{p['ad_count']}</td>"
                f"<td>₹{int(p['spend']):,}</td>"
                f"<td>{p['impressions']:,}</td>"
                f"<td>{p['clicks']:,}</td>"
                f"<td>{p['lpv']:,}</td>"
                f"<td>{p['purchases']}</td>"
                f"<td>{p['cvr']:.2f}%</td></tr>"
            )
        return ('<table>'
                '<thead><tr><th>#</th><th>URL</th><th># Ads</th><th>Spend</th>'
                '<th>Imp</th><th>Clicks</th><th>LPVs</th><th>Purchases</th><th>CVR</th></tr></thead>'
                f'<tbody>{body}</tbody></table>')

    def render_new_pushed(ads):
        new_ads = section_new_creatives(ads, recent_ad_ids)
        if not new_ads:
            return f'<p class="empty">No new ads pushed in last {NEW_CREATIVE_DAYS} days for this category</p>'
        body = ''
        for a in new_ads:
            body += (
                f"<tr><td>{a.get('_created', '—')}</td>"
                f"<td>{a.get('_status', '—')}</td>"
                f"<td style='font-size:11px'>{a['ad_name'][:60]}</td>"
                f"<td>{a['type']}</td>"
                f"<td>₹{int(a['spend']):,}</td>"
                f"<td>{a['ctr']:.2f}%</td>"
                f"<td>{a['purchases']}</td>"
                f"<td>₹{int(a['revenue']):,}</td>"
                f"<td class='{cls(a['roas'])}'>{a['roas']:.2f}x</td></tr>"
            )
        return ('<table>'
                '<thead><tr><th>Created</th><th>Status</th><th>Ad Name</th><th>Type</th>'
                '<th>Spend</th><th>CTR</th><th>Purch.</th><th>Revenue</th><th>ROAS</th></tr></thead>'
                f'<tbody>{body}</tbody></table>')

    def render_budget(category):
        rows = ''
        ts_total = ts_rev = ts_s = ts_r = ts_sr = ts_rr = 0
        for ds in sorted(history.keys()):
            slot = history[ds].get(category, {'sales': 0, 'retarget': 0, 'rev_sales': 0, 'rev_retarget': 0})
            s_spend, s_rev = slot['sales'], slot['rev_sales']
            r_spend, r_rev = slot['retarget'], slot['rev_retarget']
            t_spend = s_spend + r_spend; t_rev = s_rev + r_rev
            ts_total += t_spend; ts_rev += t_rev
            ts_s += s_spend; ts_r += r_spend; ts_sr += s_rev; ts_rr += r_rev
            s_roas = (s_rev / s_spend) if s_spend else 0
            r_roas = (r_rev / r_spend) if r_spend else 0
            t_roas = (t_rev / t_spend) if t_spend else 0
            rows += (
                f"<tr><td>{ds}</td>"
                f"<td>₹{int(s_spend):,}</td><td>₹{int(s_rev):,}</td>"
                f"<td class='{cls(s_roas)}'>{(f'{s_roas:.2f}x' if s_spend else '—')}</td>"
                f"<td>₹{int(r_spend):,}</td><td>₹{int(r_rev):,}</td>"
                f"<td class='{cls(r_roas)}'>{(f'{r_roas:.2f}x' if r_spend else '—')}</td>"
                f"<td>₹{int(t_spend):,}</td>"
                f"<td class='{cls(t_roas)}'>{(f'{t_roas:.2f}x' if t_spend else '—')}</td></tr>"
            )
        # Total row
        ts_s_roas = (ts_sr / ts_s) if ts_s else 0
        ts_r_roas = (ts_rr / ts_r) if ts_r else 0
        ts_t_roas = (ts_rev / ts_total) if ts_total else 0
        rows += (
            f"<tr style='background:#eef2ff;font-weight:700'>"
            f"<td>{BUDGET_HISTORY_DAYS}-day total</td>"
            f"<td>₹{int(ts_s):,}</td><td>₹{int(ts_sr):,}</td>"
            f"<td class='{cls(ts_s_roas)}'>{(f'{ts_s_roas:.2f}x' if ts_s else '—')}</td>"
            f"<td>₹{int(ts_r):,}</td><td>₹{int(ts_rr):,}</td>"
            f"<td class='{cls(ts_r_roas)}'>{(f'{ts_r_roas:.2f}x' if ts_r else '—')}</td>"
            f"<td>₹{int(ts_total):,}</td>"
            f"<td class='{cls(ts_t_roas)}'>{(f'{ts_t_roas:.2f}x' if ts_total else '—')}</td></tr>"
        )
        return ('<table>'
                '<thead><tr><th>Date</th>'
                '<th>Sales Spend</th><th>Sales Rev</th><th>Sales ROAS</th>'
                '<th>Retarget Spend</th><th>Retarget Rev</th><th>Retarget ROAS</th>'
                '<th>Total Spend</th><th>Overall ROAS</th></tr></thead>'
                f'<tbody>{rows}</tbody></table>')

    # Build all category panels
    panels_html = ''
    tabs_html   = ''
    for i, cat in enumerate(CATEGORIES):
        cat_ads   = per_cat_ads.get(cat, [])
        cat_spend = int(sum(a['spend'] for a in cat_ads))
        slug = cat.replace(' ', '-').lower()
        active = ' active' if i == 0 else ''
        hidden = '' if i == 0 else ' hidden'
        tabs_html += (
            f'<button class="cat-tab{active}" data-cat="{slug}">'
            f'{cat} <span class="badge">{len(cat_ads)}</span></button>'
        )
        # Summary metrics for the card grid at top of the panel
        s = compute_category_summary(cat_ads)
        roas_cls = cls(s['roas'])
        summary_grid = f'''
  <div class="summary-grid">
    <div class="sumcard sumcard-ads"><div class="sumcard-icon">📊</div>
      <div class="sumcard-lbl">Active Ads</div>
      <div class="sumcard-val">{s['active_ads']}</div>
      <div class="sumcard-sub">with spend today</div></div>
    <div class="sumcard sumcard-spend"><div class="sumcard-icon">💰</div>
      <div class="sumcard-lbl">Spend Today</div>
      <div class="sumcard-val">₹{int(s['spend']):,}</div>
      <div class="sumcard-sub">{s['purchases']:,} purchases</div></div>
    <div class="sumcard sumcard-rev"><div class="sumcard-icon">💵</div>
      <div class="sumcard-lbl">Revenue</div>
      <div class="sumcard-val">₹{int(s['revenue']):,}</div>
      <div class="sumcard-sub">CVR {s['cvr']:.2f}%</div></div>
    <div class="sumcard sumcard-roas"><div class="sumcard-icon">🎯</div>
      <div class="sumcard-lbl">ROAS</div>
      <div class="sumcard-val {roas_cls}">{s['roas']:.2f}x</div>
      <div class="sumcard-sub">spend-weighted</div></div>
    <div class="sumcard sumcard-ctr"><div class="sumcard-icon">📈</div>
      <div class="sumcard-lbl">CTR</div>
      <div class="sumcard-val">{s['ctr']:.2f}%</div>
      <div class="sumcard-sub">{s['clicks']:,} link clicks</div></div>
    <div class="sumcard sumcard-cpm"><div class="sumcard-icon">📢</div>
      <div class="sumcard-lbl">CPM</div>
      <div class="sumcard-val">₹{int(s['cpm']):,}</div>
      <div class="sumcard-sub">{s['impressions']:,} imp</div></div>
    <div class="sumcard sumcard-cpv"><div class="sumcard-icon">🎬</div>
      <div class="sumcard-lbl">CPV</div>
      <div class="sumcard-val">{('₹' + f"{s['cpv']:.2f}") if s['cpv'] else '—'}</div>
      <div class="sumcard-sub">{s['thruplay']:,} thruplays</div></div>
    <div class="sumcard sumcard-cpr"><div class="sumcard-icon">👥</div>
      <div class="sumcard-lbl">CPR / 1k Reach</div>
      <div class="sumcard-val">₹{int(s['cpr_1k']):,}</div>
      <div class="sumcard-sub">{s['reach']:,} reach</div></div>
  </div>'''
        panels_html += f'''
<div class="cat-panel" data-cat="{slug}"{hidden}>
  <div class="panel-header">
    <h2>📂 {cat}</h2>
    <div class="meta">{len(cat_ads)} ads with spend today · ₹{cat_spend:,} total</div>
  </div>

  {summary_grid}

  <section class="card">
    <h3 class="sec-best">🏆 Top 10 Creatives by ROAS <span class="meta">— min ₹{MIN_SPEND_FOR_TOP} spend</span></h3>
    {render_section_best_worst(cat_ads, 'best', 'best')}
  </section>

  <section class="card">
    <h3 class="sec-worst">🚨 Bottom 10 Creatives by ROAS <span class="meta">— min ₹{MIN_SPEND_FOR_TOP} spend</span></h3>
    {render_section_best_worst(cat_ads, 'worst', 'worst')}
  </section>

  <section class="card">
    <h3 class="sec-pages">🔗 Landing Pages in Use <span class="meta">— spend grouped by URL</span></h3>
    {render_landing_pages(cat_ads)}
  </section>

  <section class="card">
    <h3 class="sec-new">🆕 New Creatives Pushed <span class="meta">— last {NEW_CREATIVE_DAYS} days</span></h3>
    {render_new_pushed(cat_ads)}
  </section>

  <section class="card">
    <h3 class="sec-budget">💰 Budget &amp; Spend Summary <span class="meta">— last {BUDGET_HISTORY_DAYS} days · Sales vs Retarget</span></h3>
    {render_budget(cat)}
  </section>
</div>
'''

    html = f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>📂 Category Heads — Meta Ads</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:'Segoe UI',system-ui,sans-serif;background:#f0f4fb;color:#1a1a2e}}
.header{{background:linear-gradient(135deg,#0d2145,#1a3d7c);padding:20px 28px;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:12px}}
.header h1{{color:#fff;font-size:20px;font-weight:700}}
.header p{{color:rgba(255,255,255,.6);font-size:11px;margin-top:3px}}
.header nav{{color:rgba(255,255,255,.85);font-size:12px}}
.header nav a{{color:#fff;text-decoration:none;border-bottom:1px dashed rgba(255,255,255,.5);margin-left:14px}}
.header nav a:hover{{border-style:solid}}
.tabs{{background:#fff;border-bottom:1px solid #dde3f0;padding:10px 28px;display:flex;gap:6px;flex-wrap:wrap;position:sticky;top:0;z-index:50;box-shadow:0 2px 8px rgba(0,0,0,.05)}}
.cat-tab{{padding:7px 14px;border-radius:20px;border:2px solid #dde3f0;background:#fff;color:#6b7280;font-size:12px;font-weight:600;cursor:pointer;transition:all .15s;display:inline-flex;align-items:center;gap:6px}}
.cat-tab:hover{{border-color:#1a3d7c;color:#1a3d7c}}
.cat-tab.active{{background:#1a3d7c;color:#fff;border-color:#1a3d7c}}
.cat-tab .badge{{background:rgba(255,255,255,.2);padding:1px 7px;border-radius:10px;font-size:10px;font-weight:700}}
.cat-tab.active .badge{{background:rgba(255,255,255,.3)}}
.main{{max-width:1400px;margin:0 auto;padding:24px 28px}}
.cat-panel[hidden]{{display:none}}
.panel-header{{margin-bottom:16px}}
.panel-header h2{{font-size:22px;color:#0d2145;font-weight:800}}
.panel-header .meta{{color:#6b7280;font-size:12px;margin-top:4px}}
.summary-grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:18px}}
@media(max-width:900px){{.summary-grid{{grid-template-columns:repeat(2,1fr)}}}}
.sumcard{{background:#fff;border-radius:10px;padding:14px 16px;border:1px solid #dde3f0;box-shadow:0 2px 8px rgba(0,0,0,.04);position:relative;overflow:hidden;transition:transform .15s}}
.sumcard:hover{{transform:translateY(-1px)}}
.sumcard::before{{content:'';position:absolute;top:0;left:0;width:3px;height:100%}}
.sumcard-ads::before{{background:#1a3d7c}}
.sumcard-spend::before{{background:#f5c518}}
.sumcard-rev::before{{background:#00a878}}
.sumcard-roas::before{{background:#9b59b6}}
.sumcard-ctr::before{{background:#3498db}}
.sumcard-cpm::before{{background:#e67e22}}
.sumcard-cpv::before{{background:#16a085}}
.sumcard-cpr::before{{background:#7f8c8d}}
.sumcard-icon{{font-size:18px;margin-bottom:4px}}
.sumcard-lbl{{font-size:9px;color:#6b7280;text-transform:uppercase;letter-spacing:.6px;font-weight:700}}
.sumcard-val{{font-size:22px;font-weight:900;color:#0d2145;margin:3px 0 2px}}
.sumcard-val.rg{{color:#0d6e3a}}.sumcard-val.ro{{color:#a35a00}}.sumcard-val.rr{{color:#a3260a}}
.sumcard-sub{{font-size:10px;color:#9ca3af}}
.card{{background:#fff;border-radius:12px;padding:18px;border:1px solid #dde3f0;box-shadow:0 2px 10px rgba(0,0,0,.04);margin-bottom:18px}}
.card h3{{font-size:13px;font-weight:700;margin-bottom:14px;padding-bottom:8px;border-bottom:2px solid #eef2ff;color:#0d2145}}
.card h3 .meta{{color:#6b7280;font-weight:400;font-size:11px;margin-left:8px}}
.sec-best{{color:#0d6e3a}}
.sec-worst{{color:#a3260a}}
.sec-pages{{color:#5a3a8e}}
.sec-new{{color:#a35a00}}
.sec-budget{{color:#206040}}
table{{width:100%;border-collapse:collapse;font-size:12px}}
th{{background:#0d2145;color:#fff;padding:8px 10px;text-align:left;font-size:10px;letter-spacing:.3px;text-transform:uppercase}}
th:not(:first-child){{text-align:center}}
td{{padding:7px 10px;border-bottom:1px solid #eef2ff}}
td:not(:first-child){{text-align:center;font-weight:600}}
td a{{color:#1a3d7c;text-decoration:none;border-bottom:1px dotted #1a3d7c}}
.rg{{color:#0d6e3a;font-weight:700}}
.ro{{color:#a35a00;font-weight:700}}
.rr{{color:#a3260a;font-weight:700}}
.empty{{color:#888;font-size:12px;text-align:center;padding:20px;font-style:italic}}
footer{{text-align:center;color:#6b7280;font-size:11px;padding:20px}}
footer a{{color:#1a3d7c;text-decoration:none;border-bottom:1px dashed #1a3d7c}}
</style>
</head>
<body>

<div class="header">
  <div>
    <h1>📂 Category Heads — Meta Ads</h1>
    <p>{datetime.strptime(date_str,'%Y-%m-%d').strftime('%A, %d %B %Y')} · SM portal · per-category live data</p>
  </div>
  <nav>
    <a href="/">🏠 NTN Dashboard</a>
    <a href="/today_live.html">🔴 Today Live</a>
  </nav>
</div>

<div class="tabs">
  <span style="font-size:11px;font-weight:700;color:#6b7280;text-transform:uppercase;letter-spacing:.5px;align-self:center;margin-right:8px">Category</span>
  {tabs_html}
</div>

<div class="main">
{panels_html}
</div>

<footer>
  🤖 Auto-rebuilds with the daily pipeline (04:30 IST) · last update {ts_label} · <a href="{sheet_url}" target="_blank">Sheet source</a>
</footer>

<script>
document.querySelectorAll('.cat-tab').forEach(btn => {{
  btn.addEventListener('click', () => {{
    const slug = btn.dataset.cat;
    document.querySelectorAll('.cat-tab').forEach(b => b.classList.toggle('active', b === btn));
    document.querySelectorAll('.cat-panel').forEach(p => {{
      p.hidden = p.dataset.cat !== slug;
    }});
    window.scrollTo({{top: 0, behavior: 'smooth'}});
  }});
}});
</script>

</body>
</html>
'''

    out_path = OUT_DIR / 'categories.html'
    out_path.write_text(html, encoding='utf-8')
    print(f"   ✅ Wrote {out_path}")


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

    # History caching — fetching 7 days × 7 accounts is ~50 API calls; expensive
    # to re-do every hourly run. Daily run does the full fetch and saves it to
    # state/category_history.json (which the state-sync action persists across
    # runs via the orphan `state` branch). Hourly runs use --no-history to load
    # the cached file instead.
    STATE_DIR = Path(os.environ.get('META_REPORTS_STATE_DIR') or (_REPO_ROOT / 'state'))
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    HISTORY_CACHE = STATE_DIR / 'category_history.json'

    history = {}
    if args.no_history:
        if HISTORY_CACHE.exists():
            try:
                history = json.loads(HISTORY_CACHE.read_text())
                # JSON converts inner defaultdicts to plain dicts — that's fine
                print(f"   📂 Loaded cached history ({len(history)} day(s)) from {HISTORY_CACHE}")
            except Exception as e:
                print(f"   ⚠️  Could not read cached history ({e}); budget section will be empty")
        else:
            print(f"   ⚠️  --no-history but no cache at {HISTORY_CACHE}; budget section will be empty")
    else:
        print(f"→ Fetching {BUDGET_HISTORY_DAYS}-day history per category × type…")
        history = fetch_category_history(args.date, days=BUDGET_HISTORY_DAYS)
        print(f"   {len(history)} day(s) loaded")
        # Convert defaultdicts → plain dicts for JSON serialisation
        plain = {ds: {cat: dict(slots) for cat, slots in cat_dict.items()}
                 for ds, cat_dict in history.items()}
        HISTORY_CACHE.write_text(json.dumps(plain))
        print(f"   💾 Cached to {HISTORY_CACHE}")

    target_cats = [args.category] if args.category else CATEGORIES
    per_cat_ads = {}
    for cat in target_cats:
        cat_ads = [a for a in ads if a['category'] == cat]
        per_cat_ads[cat] = cat_ads
        print(f"\n→ Building tab for {cat} ({len(cat_ads)} ads)…")
        rows = build_category_rows(cat, cat_ads, link_map, recent_ad_ids, history, ts_label)
        write_to_sheet(cat, rows, ts_label)
        time.sleep(1)  # avoid sheet API rate limits

    # Also render an HTML dashboard with all categories — for Cloudflare Pages.
    # Always render the full set (all CATEGORIES), even when --category was used,
    # so the dashboard stays consistent.
    if not args.category:
        print("\n→ Rendering HTML dashboard (out/categories.html)…")
        sheet_url = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}"
        render_categories_dashboard_html(per_cat_ads, link_map, recent_ad_ids,
                                         history, ts_label, args.date, sheet_url)

    print(f"\n🎉 Done — {len(target_cats)} category tab(s) refreshed")


if __name__ == '__main__':
    main()
