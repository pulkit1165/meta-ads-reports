#!/usr/bin/env python3
"""One-off: today's ad-level success rate across all 3 portals.

Usage:
  python scripts/creative_success_rate.py [YYYY-MM-DD] [--min-spend=N]

Outputs:
  - Pivot to stdout (ROAS buckets 1.5/2/2.5/3+, sum spend, avg ROAS, #ads)
  - CSV of qualifying ads (ROAS >= 1.5) with ad_id, ads_manager_url, preview_link
    saved to out/creative_success_rate_<date>.csv
"""
import os, sys, json, time, csv, requests
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

_REPO = Path(__file__).resolve().parent.parent
load_dotenv(_REPO / '.env')

TOKEN = os.getenv('META_ACCESS_TOKEN')
GRAPH = 'https://graph.facebook.com/v19.0'
IST = ZoneInfo('Asia/Kolkata')
OUT_DIR = _REPO / 'out'
OUT_DIR.mkdir(parents=True, exist_ok=True)

PORTAL_ACCOUNTS = {
    'NBP': [
        (os.getenv('NBP_SKIN'),         'NBP Skin'),
        (os.getenv('NBP_HAIR_PERFUME'), 'NBP Hair Perfume'),
        (os.getenv('NBP_CRYSTALS'),     'NBP Crystals'),
    ],
    'SM': [
        (os.getenv('SM_FRAGRANCE_01'),   'SM Fragrance 01'),
        (os.getenv('SM_SKIN'),           'SM Skin'),
        (os.getenv('SM_HAIR'),           'SM Hair'),
        (os.getenv('SM_CRYSTALS'),       'SM Crystals'),
        (os.getenv('SM_PERFUME'),        'SM Perfume'),
        (os.getenv('SM_CREDIT_LINE_05'), 'SM CL 05'),
        (os.getenv('SM_CREDIT_LINE_06'), 'SM CL 06'),
    ],
    'SML': [
        (os.getenv('SML_SKIN'),     'SML Skin'),
        (os.getenv('SML_HAIR'),     'SML Hair'),
        (os.getenv('SML_CRYSTALS'), 'SML Crystals'),
        (os.getenv('SML_CL_06'),    'SML CL 06'),
        (os.getenv('SML_CL_07'),    'SML CL 07'),
    ],
}


def safe_float(v):
    try: return float(v)
    except: return 0.0


def extract_roas(roas_field):
    if not roas_field: return 0.0
    if isinstance(roas_field, list):
        for item in roas_field:
            v = item.get('value', 0)
            return safe_float(v)
    return 0.0


def paginate(url, params, max_pages=50):
    rows, page = [], 0
    params = {**params, 'access_token': TOKEN}
    while page < max_pages:
        r = requests.get(url, params=params, timeout=30).json()
        if 'error' in r:
            err = r['error']
            if err.get('code') == 17:
                time.sleep(60); continue
            print(f"  [warn] {err.get('message','API error')[:120]}", file=sys.stderr)
            return rows
        rows.extend(r.get('data', []))
        nxt = r.get('paging', {}).get('next')
        if not nxt: break
        url, params = nxt, {}
        page += 1
    return rows


def fetch_ads_for_account(acct, acct_name, date_str):
    rows = paginate(f"{GRAPH}/{acct}/insights", {
        'level': 'ad',
        'fields': 'ad_id,ad_name,spend,purchase_roas',
        'time_range': json.dumps({'since': date_str, 'until': date_str}),
        'filtering': json.dumps([{'field': 'spend', 'operator': 'GREATER_THAN', 'value': '0'}]),
        'limit': 500,
    })
    acct_num = acct.replace('act_', '') if acct else ''
    out = []
    for r in rows:
        ad_id = r.get('ad_id')
        ads_manager_url = (
            f"https://business.facebook.com/adsmanager/manage/ads?"
            f"act={acct_num}&selected_ad_ids={ad_id}"
        ) if ad_id and acct_num else ''
        out.append({
            'ad_id': ad_id,
            'name': r.get('ad_name', ''),
            'account': acct_name,
            'acct_id': acct,
            'spend': safe_float(r.get('spend')),
            'roas': extract_roas(r.get('purchase_roas')),
            'ads_manager_url': ads_manager_url,
            'preview_link': '',
        })
    return out


def fetch_preview_link(ad_id):
    """Best-effort: returns preview_shareable_link or '' if unavailable."""
    try:
        r = requests.get(
            f"{GRAPH}/{ad_id}",
            params={'fields': 'preview_shareable_link', 'access_token': TOKEN},
            timeout=20,
        ).json()
        return r.get('preview_shareable_link', '') or ''
    except Exception:
        return ''


def bucket_label(roas):
    if roas < 1.5: return 'Below 1.5'
    if roas < 2.0: return 'Roas >1.5'
    if roas < 2.5: return 'Roas >2'
    if roas < 3.0: return 'Roas >2.5'
    return 'Roas >3'


def main():
    args = list(sys.argv[1:])
    date_str = datetime.now(IST).strftime('%Y-%m-%d')
    min_spend = 500.0
    fetch_previews = True
    for a in args:
        if a.startswith('--min-spend='):
            min_spend = float(a.split('=', 1)[1])
        elif a == '--no-previews':
            fetch_previews = False
        else:
            date_str = a
    print(f"Date: {date_str} (IST)   min_spend: {min_spend}\n")

    all_ads = []
    for portal, accounts in PORTAL_ACCOUNTS.items():
        for acct, acct_name in accounts:
            if not acct:
                continue
            ads = fetch_ads_for_account(acct, acct_name, date_str)
            for a in ads:
                a['portal'] = portal
            all_ads.extend(ads)
            print(f"  {portal}/{acct_name}: {len(ads)} ads")

    print(f"\nTotal ads with spend>0: {len(all_ads)}")
    filtered = [a for a in all_ads if a['spend'] >= min_spend]
    print(f"After spend>={min_spend} filter: {len(filtered)} ads")

    # Pivot
    buckets = [
        ('Roas >1.5', 1.5, 2.0),
        ('Roas >2',   2.0, 2.5),
        ('Roas >2.5', 2.5, 3.0),
        ('Roas >3',   3.0, float('inf')),
    ]
    below = [a for a in filtered if a['roas'] < 1.5]

    print(f"\n{'Row Labels':<14}{'Sum Spend (INR)':>20}{'Avg ROAS':>14}{'#Ads':>8}")
    print('-' * 56)
    total_spend = total_roas_sum = 0.0
    total_n = 0
    for label, lo, hi in buckets:
        in_bucket = [a for a in filtered if lo <= a['roas'] < hi]
        spend_sum = sum(a['spend'] for a in in_bucket)
        avg_roas = (sum(a['roas'] for a in in_bucket) / len(in_bucket)) if in_bucket else 0.0
        print(f"{label:<14}{spend_sum:>20,.2f}{avg_roas:>14.6f}{len(in_bucket):>8}")
        total_spend += spend_sum
        total_roas_sum += sum(a['roas'] for a in in_bucket)
        total_n += len(in_bucket)
    grand_avg = (total_roas_sum / total_n) if total_n else 0.0
    print('-' * 56)
    print(f"{'Grand Total':<14}{total_spend:>20,.2f}{grand_avg:>14.6f}{total_n:>8}")
    if below:
        bs = sum(a['spend'] for a in below)
        ba = sum(a['roas'] for a in below) / len(below)
        print(f"\n(context) Below 1.5: spend {bs:,.2f}, avg ROAS {ba:.6f}, n={len(below)}")

    # Per-ad CSV for qualifying ads (ROAS >= 1.5)
    qualifying = sorted(
        [a for a in filtered if a['roas'] >= 1.5],
        key=lambda x: x['roas'], reverse=True,
    )
    if fetch_previews and qualifying:
        print(f"\nFetching preview links for {len(qualifying)} qualifying ads...")
        for i, a in enumerate(qualifying, 1):
            a['preview_link'] = fetch_preview_link(a['ad_id'])
            if i % 25 == 0:
                print(f"  {i}/{len(qualifying)}")

    suffix = f"_minspend{int(min_spend)}" if min_spend != 500 else ''
    csv_path = OUT_DIR / f"creative_success_rate_{date_str}{suffix}.csv"
    with open(csv_path, 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow([
            'bucket', 'roas', 'spend_inr', 'ad_id', 'ad_name',
            'portal', 'account', 'ads_manager_url', 'preview_link',
        ])
        for a in qualifying:
            w.writerow([
                bucket_label(a['roas']),
                f"{a['roas']:.4f}",
                f"{a['spend']:.2f}",
                a['ad_id'],
                a['name'],
                a['portal'],
                a['account'],
                a['ads_manager_url'],
                a['preview_link'],
            ])
    print(f"\nCSV: {csv_path}  ({len(qualifying)} rows)")


if __name__ == '__main__':
    main()
