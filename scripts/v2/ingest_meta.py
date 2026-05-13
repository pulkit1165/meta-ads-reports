#!/usr/bin/env python3
"""
NTN Dashboard v2 — Meta API ingestion into SQLite.

Pulls all SM/SML/NBP ad accounts' campaigns + ad-level insights for a
given date and writes them into state/ntn.db. Idempotent (UPSERT).
Rate-limit aware — handles Meta's 5+ different rate-limit codes with
exponential backoff. Logs every run to the ingest_log table.

Usage:
  python3 scripts/v2/ingest_meta.py                 # ingest yesterday IST
  python3 scripts/v2/ingest_meta.py --date 2026-05-05
  python3 scripts/v2/ingest_meta.py --portal SM
  python3 scripts/v2/ingest_meta.py --days 7        # backfill last 7 days
  python3 scripts/v2/ingest_meta.py --campaigns-only # skip ad insights
"""

import argparse
import json
import os
import sys
import traceback
from datetime import datetime, timedelta
from pathlib import Path

# Add v2 dir to path so we can import _utils
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _utils import (  # noqa: E402
    db_connect, IST, GRAPH_API, PORTAL_ACCOUNTS, now_iso,
    safe_float, safe_int, meta_get, meta_paginate,
    log_ingest_start, log_ingest_finish, MetaRateLimitError,
)

# Action types we extract from the `actions` array
PURCHASE_ACTIONS = {'omni_purchase', 'purchase'}
ATC_ACTIONS      = {'omni_add_to_cart', 'add_to_cart'}
LPV_ACTIONS      = {'landing_page_view'}
IC_ACTIONS       = {'omni_initiated_checkout', 'initiate_checkout'}
OUTBOUND_ACTIONS = {'outbound_click'}


def action_sum(actions, types):
    if not actions: return 0
    return sum(safe_int(a.get('value', 0)) for a in actions
               if a.get('action_type') in types)


def action_value_sum(action_values, types):
    if not action_values: return 0.0
    return sum(safe_float(a.get('value', 0)) for a in action_values
               if a.get('action_type') in types)


def extract_roas(raw, prefer='value'):
    """Meta returns purchase_roas as a list of dicts. We pull a specific key."""
    if not raw or not isinstance(raw, list):
        return None
    for item in raw:
        if not isinstance(item, dict):
            continue
        v = item.get(prefer)
        if v not in (None, '', '0', 0):
            try: return float(v)
            except (TypeError, ValueError): pass
    # Fallback to whatever's first
    for item in raw:
        if isinstance(item, dict):
            for k in ('value', '1d_click', '7d_click'):
                v = item.get(k)
                if v not in (None, '', '0', 0):
                    try: return float(v)
                    except (TypeError, ValueError): pass
    return None


# ── Campaign metadata ─────────────────────────────────────────────────────
def fetch_campaigns(account_id: str):
    """Pulls all campaigns for an ad account (active + paused, all statuses).
    Includes start_time + daily_budget for the long-runners section."""
    return meta_paginate(
        f'{GRAPH_API}/{account_id}/campaigns',
        {
            'fields': 'id,name,status,effective_status,objective,start_time,'
                      'stop_time,daily_budget,lifetime_budget',
            'limit': 200,
        },
    )


def upsert_campaigns(conn, portal: str, account_id: str, camps: list):
    """Idempotent UPSERT. daily_budget is INR (Meta returns paise → /100)."""
    rows = []
    for c in camps:
        rows.append((
            c.get('id'),
            portal,
            account_id,
            c.get('name'),
            c.get('status'),
            c.get('effective_status'),
            c.get('objective'),
            c.get('start_time'),
            c.get('stop_time'),
            safe_float(c.get('daily_budget')) / 100 if c.get('daily_budget') else None,
            safe_float(c.get('lifetime_budget')) / 100 if c.get('lifetime_budget') else None,
            now_iso(),
        ))
    if not rows: return 0
    conn.executemany(
        '''INSERT INTO meta_campaigns
           (campaign_id, portal, account_id, name, status, effective_status,
            objective, start_time, stop_time, daily_budget, lifetime_budget,
            last_synced)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(campaign_id) DO UPDATE SET
             portal=excluded.portal,
             account_id=excluded.account_id,
             name=excluded.name,
             status=excluded.status,
             effective_status=excluded.effective_status,
             objective=excluded.objective,
             start_time=COALESCE(excluded.start_time, meta_campaigns.start_time),
             stop_time=excluded.stop_time,
             daily_budget=excluded.daily_budget,
             lifetime_budget=excluded.lifetime_budget,
             last_synced=excluded.last_synced''',
        rows
    )
    return len(rows)


# ── Ad-level daily insights ───────────────────────────────────────────────
INSIGHT_FIELDS = (
    'ad_id,ad_name,campaign_id,campaign_name,adset_id,adset_name,'
    'spend,impressions,reach,clicks,inline_link_clicks,outbound_clicks,'
    'ctr,cpm,cpc,frequency,'
    'actions,action_values,'
    'purchase_roas,'
    'video_p25_watched_actions,video_p50_watched_actions,'
    'video_p75_watched_actions,video_thruplay_watched_actions,'
    'video_avg_time_watched_actions'
)


def fetch_ad_insights(account_id: str, date_str: str):
    """Ad-level insights for a single day. Filters to ads with spend>0."""
    return meta_paginate(
        f'{GRAPH_API}/{account_id}/insights',
        {
            'level': 'ad',
            'fields': INSIGHT_FIELDS,
            'time_range': json.dumps({'since': date_str, 'until': date_str}),
            'filtering': json.dumps([
                {'field': 'spend', 'operator': 'GREATER_THAN', 'value': '0'},
            ]),
            'limit': 250,
        },
    )


def parse_ad_row(r: dict, portal: str, account_id: str, account_name: str,
                 date_str: str):
    """Turn one Meta insights row into a dict matching meta_ads_daily columns."""
    actions = r.get('actions') or []
    avals = r.get('action_values') or []
    spend = safe_float(r.get('spend'))
    purchases = action_sum(actions, PURCHASE_ACTIONS)
    revenue   = action_value_sum(avals, PURCHASE_ACTIONS)
    return {
        'date':         date_str,
        'ad_id':        r.get('ad_id'),
        'portal':       portal,
        'account_id':   account_id,
        'account_name': account_name,
        'campaign_id':   r.get('campaign_id'),
        'campaign_name': r.get('campaign_name'),
        'adset_id':      r.get('adset_id'),
        'adset_name':    r.get('adset_name'),
        'ad_name':       r.get('ad_name'),
        'spend':         spend,
        'impressions':   safe_int(r.get('impressions')),
        'reach':         safe_int(r.get('reach')),
        'clicks':        safe_int(r.get('clicks')),
        'inline_link_clicks':   safe_int(r.get('inline_link_clicks')),
        'outbound_clicks':      action_sum(r.get('outbound_clicks'), OUTBOUND_ACTIONS),
        'ctr':           safe_float(r.get('ctr')) or None,
        'cpm':           safe_float(r.get('cpm')) or None,
        'cpc':           safe_float(r.get('cpc')) or None,
        'frequency':     safe_float(r.get('frequency')) or None,
        'purchases':     purchases,
        'revenue':       revenue,
        'roas':          (revenue / spend) if spend > 0 and revenue > 0 else None,
        'purchase_roas_default':  extract_roas(r.get('purchase_roas'), 'value'),
        'purchase_roas_1d_click': extract_roas(r.get('purchase_roas'), '1d_click'),
        'purchase_roas_7d_click': extract_roas(r.get('purchase_roas'), '7d_click'),
        'landing_page_views': action_sum(actions, LPV_ACTIONS),
        'add_to_cart':   action_sum(actions, ATC_ACTIONS),
        'initiate_checkout': action_sum(actions, IC_ACTIONS),
        'video_p25_views':  action_sum(r.get('video_p25_watched_actions'), {'video_view'}),
        'video_p50_views':  action_sum(r.get('video_p50_watched_actions'), {'video_view'}),
        'video_p75_views':  action_sum(r.get('video_p75_watched_actions'), {'video_view'}),
        'video_thruplay':   action_sum(r.get('video_thruplay_watched_actions'), {'video_view'}),
        'video_avg_time_watched_sec':
            (action_sum(r.get('video_avg_time_watched_actions'), {'video_view'})
             if r.get('video_avg_time_watched_actions') else None),
        'fetched_at':    now_iso(),
    }


def upsert_ads_daily(conn, ads: list):
    if not ads: return 0
    cols = list(ads[0].keys())
    placeholders = ','.join(['?'] * len(cols))
    update_cols = [c for c in cols if c not in ('date', 'ad_id')]
    update_clause = ','.join(f'{c}=excluded.{c}' for c in update_cols)
    sql = (
        f'INSERT INTO meta_ads_daily({",".join(cols)}) VALUES({placeholders}) '
        f'ON CONFLICT(date, ad_id) DO UPDATE SET {update_clause}'
    )
    rows = [tuple(a[c] for c in cols) for a in ads]
    conn.executemany(sql, rows)
    return len(rows)


# ── Ad lifetime metadata (rolled from daily) ─────────────────────────────
def refresh_meta_ads_meta(conn, ad_ids: list):
    """For each ad in `ad_ids`, recompute first_seen/last_seen/days_active/
    total_spend/total_revenue/total_purchases from meta_ads_daily.
    Leaves classification fields (category, etc.) untouched — those are
    populated by classify_ads.py.
    """
    if not ad_ids: return 0
    placeholders = ','.join(['?'] * len(ad_ids))
    rows = conn.execute(
        f'''SELECT ad_id, MIN(date) AS first_seen, MAX(date) AS last_seen,
                   COUNT(*) AS days_active,
                   SUM(spend) AS total_spend,
                   SUM(revenue) AS total_revenue,
                   SUM(purchases) AS total_purchases,
                   MAX(portal) AS portal,
                   MAX(account_id) AS account_id,
                   MAX(campaign_id) AS campaign_id,
                   MAX(adset_id) AS adset_id,
                   MAX(ad_name) AS ad_name
            FROM meta_ads_daily
            WHERE ad_id IN ({placeholders}) AND spend > 0
            GROUP BY ad_id''',
        ad_ids
    ).fetchall()
    if not rows: return 0
    conn.executemany(
        '''INSERT INTO meta_ads_meta
           (ad_id, first_seen, last_seen, days_active, total_spend,
            total_revenue, total_purchases, portal, account_id, campaign_id,
            adset_id, ad_name)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(ad_id) DO UPDATE SET
             first_seen=MIN(meta_ads_meta.first_seen, excluded.first_seen),
             last_seen=MAX(meta_ads_meta.last_seen, excluded.last_seen),
             days_active=excluded.days_active,
             total_spend=excluded.total_spend,
             total_revenue=excluded.total_revenue,
             total_purchases=excluded.total_purchases,
             portal=COALESCE(meta_ads_meta.portal, excluded.portal),
             account_id=COALESCE(meta_ads_meta.account_id, excluded.account_id),
             campaign_id=COALESCE(meta_ads_meta.campaign_id, excluded.campaign_id),
             adset_id=COALESCE(meta_ads_meta.adset_id, excluded.adset_id),
             ad_name=COALESCE(meta_ads_meta.ad_name, excluded.ad_name)''',
        rows
    )
    return len(rows)


# ── Main ingest pipeline for a single date ───────────────────────────────
def ingest_for_date(conn, date_str: str, portals: list, *,
                    campaigns_only: bool = False):
    total_camps = 0
    total_ads = 0
    affected_ad_ids = set()

    started = log_ingest_start(conn, 'ingest_meta', date_str)
    try:
        for portal in portals:
            print(f"\n→ Portal: {portal}")
            for env_key, account_name in PORTAL_ACCOUNTS[portal]:
                account_id = os.environ.get(env_key)
                if not account_id:
                    print(f"  ⚠️  {env_key} not set — skipping {account_name}")
                    continue
                print(f"  · {account_name} ({account_id})")

                # 1. Campaign metadata (lightweight, always pull)
                try:
                    camps = fetch_campaigns(account_id)
                    n = upsert_campaigns(conn, portal, account_id, camps)
                    total_camps += n
                    print(f"     campaigns: {n} upserted")
                except MetaRateLimitError as e:
                    print(f"     campaigns: rate limit unrecoverable — {e}")
                    continue

                if campaigns_only:
                    continue

                # 2. Ad-level insights for the date
                try:
                    rows = fetch_ad_insights(account_id, date_str)
                    parsed = [
                        parse_ad_row(r, portal, account_id, account_name, date_str)
                        for r in rows if r.get('ad_id')
                    ]
                    n = upsert_ads_daily(conn, parsed)
                    total_ads += n
                    affected_ad_ids.update(p['ad_id'] for p in parsed)
                    print(f"     ads w/ spend on {date_str}: {n} upserted")
                except MetaRateLimitError as e:
                    print(f"     ads: rate limit unrecoverable — {e}")
                    continue

        # 3. Refresh lifetime ad metadata for all ads we touched
        if affected_ad_ids:
            updated = refresh_meta_ads_meta(conn, list(affected_ad_ids))
            print(f"\n📋 Refreshed meta_ads_meta for {updated} ads")

        log_ingest_finish(conn, 'ingest_meta', date_str, started,
                          status='success',
                          rows_written=total_camps + total_ads)
        print(f"\n✅ ingest_meta complete · {total_camps} camps · {total_ads} ads")
        return True
    except Exception as e:
        traceback.print_exc()
        log_ingest_finish(conn, 'ingest_meta', date_str, started,
                          status='failed',
                          rows_written=total_camps + total_ads,
                          error_message=str(e)[:500])
        print(f"\n❌ ingest_meta failed: {e}")
        return False


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--date', help='YYYY-MM-DD (default: today IST)')
    p.add_argument('--days', type=int, default=2,
                   help='Backfill last N days (default 2 = today + yesterday, '
                        'matching Shopify ingest; covers late-arriving '
                        'conversion attribution on yesterday)')
    p.add_argument('--portal', choices=['SM', 'SML', 'NBP'],
                   help='Limit to one portal (default: all 3)')
    p.add_argument('--campaigns-only', action='store_true',
                   help='Skip ad insights, only refresh campaign metadata')
    p.add_argument('--db', help='SQLite path (default: state/ntn.db)')
    args = p.parse_args()

    if not os.getenv('META_ACCESS_TOKEN'):
        print("❌ META_ACCESS_TOKEN not set in env or .env")
        sys.exit(1)

    today = datetime.now(IST).date()
    if args.date:
        end_date = datetime.strptime(args.date, '%Y-%m-%d').date()
    else:
        end_date = today

    dates = [end_date - timedelta(days=i) for i in range(args.days)]
    dates.sort()  # oldest first
    portals = [args.portal] if args.portal else ['SM', 'SML', 'NBP']

    db_path = Path(args.db) if args.db else None
    conn = db_connect(db_path) if db_path else db_connect()

    print(f"📥 Meta Ingest")
    print(f"   Dates: {[d.strftime('%Y-%m-%d') for d in dates]}")
    print(f"   Portals: {portals}")
    print(f"   DB: {db_path or 'state/ntn.db (default)'}")

    all_ok = True
    for d in dates:
        ds = d.strftime('%Y-%m-%d')
        print(f"\n══════ {ds} ══════")
        ok = ingest_for_date(conn, ds, portals,
                             campaigns_only=args.campaigns_only)
        all_ok = all_ok and ok

    conn.close()
    sys.exit(0 if all_ok else 2)


if __name__ == '__main__':
    main()
