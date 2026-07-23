#!/usr/bin/env python3
"""
daily_finals.py — frozen end-of-day totals for COMPLETED days.

A completed day's ROAS must be the whole day: all of that day's Shopify sales
over that day's final Meta spend. Two reasons the live intra-day path gets this
wrong for a finished day:

  * Spend from the last hourly snapshot is short. Snapshots are cumulative and
    the last one of the day lands a few minutes into the final hour, so it
    misses the rest of it. With top-of-the-hour pulls the last snapshot is ~:03,
    missing nearly the whole 23:00 hour — Rs4.65L snapshot vs Rs4.70L actual on
    20 Jul.
  * Shopify sales get capped at the snapshot time by the intra-day alignment,
    which also drops late orders.

For a finished day both are available exactly: Meta serves the final daily total
per account (= Ads Manager), and every Shopify order for the day is in. We
compute each day ONCE — the first build after 01:00 IST the next day, when
attribution and orders have settled — and freeze it in a JSON store on the
camp-snapshots branch. Later builds read the frozen value, so a finished day
never drifts and we don't re-hit Meta every build.

Store shape:
  { "2026-07-20": { "SM": {"sales": .., "spend": .., "orders": ..}, ... }, ... }
"""
from __future__ import annotations

import json
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from portal_hourly import PORTALS, SALES_FILTER, portal_of  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
API = 'https://graph.facebook.com/v19.0'


def _get(url, retries=3):
    delay = 2
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=45) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code < 500 or attempt == retries - 1:
                raise
        except (urllib.error.URLError, TimeoutError):
            if attempt == retries - 1:
                raise
        import time
        time.sleep(delay)
        delay *= 3


def meta_daily_spend(date: str, token: str, accounts: list[str]) -> dict:
    """{portal: final spend} for `date` from Meta's historical daily insights.

    This is the Ads Manager number, not a snapshot sum. Account name drives the
    portal mapping, same as the live path.
    """
    out = {p: 0.0 for p in PORTALS}
    for aid in accounts:
        try:
            nm = _get(f'{API}/{aid}?' + urllib.parse.urlencode(
                {'fields': 'name', 'access_token': token})).get('name', '')
            d = _get(f'{API}/{aid}/insights?' + urllib.parse.urlencode({
                'time_range': json.dumps({'since': date, 'until': date}),
                'fields': 'spend', 'access_token': token}))
        except urllib.error.HTTPError as e:
            body = ''
            try:
                body = e.read().decode()
            except Exception:
                pass
            # A permission block (403 / "NOT grant") is permanent and known —
            # skip that account, exactly like the snapshot guard. Any OTHER error
            # is transient, so refuse to freeze a partial day.
            if e.code == 403 or 'NOT grant' in body:
                print(f'  daily_finals: {aid} permanently blocked — skipping (not counted)')
                continue
            print(f'  daily_finals: {aid} spend failed for {date} (HTTP {e.code})')
            raise
        except Exception as e:
            print(f'  daily_finals: {aid} spend failed for {date} ({e})')
            raise
        p = portal_of(nm)
        if p and d.get('data'):
            out[p] += float(d['data'][0].get('spend') or 0)
    return out


def shopify_daily(ntn_db: str, date: str) -> dict:
    """{portal: {'sales': .., 'orders': ..}} — ALL of the day's orders, uncapped."""
    out = {p: {'sales': 0.0, 'orders': 0} for p in PORTALS}
    con = sqlite3.connect(f'file:{ntn_db}?mode=ro', uri=True)
    try:
        for portal, sales, orders in con.execute(
                "SELECT portal, SUM(COALESCE(total_price,0)), COUNT(*) "
                "FROM shopify_orders WHERE substr(created_at,1,10)=? AND "
                + SALES_FILTER + " GROUP BY portal", (date,)):
            if portal in out:
                out[portal] = {'sales': round(float(sales or 0), 2), 'orders': int(orders or 0)}
    finally:
        con.close()
    return out


def _configured_accounts() -> list[str]:
    import re
    root = Path(__file__).resolve().parent.parent.parent
    out = []
    for name in ('config/accounts.env', '.env'):
        p = root / name
        if p.exists():
            for line in p.read_text(errors='ignore').splitlines():
                m = re.match(r'^\s*[A-Z][A-Z0-9_]*\s*=\s*(act_\d+)\s*$', line.strip())
                if m and m.group(1) not in out:
                    out.append(m.group(1))
    return out


def load(path: str) -> dict:
    p = Path(path)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    return {}


def finals_for(store: dict, date: str) -> dict | None:
    """Frozen {portal: {sales, spend, orders, roas}} for `date`, or None."""
    day = store.get(date)
    if not day:
        return None
    out = {}
    for p in PORTALS:
        c = day.get(p, {})
        sp, spend = c.get('sales', 0.0), c.get('spend', 0.0)
        out[p] = {'rev': sp, 'spend': spend, 'orders': c.get('orders', 0),
                  'roas': round(sp / spend, 2) if spend else 0.0}
    rev = sum(out[p]['rev'] for p in PORTALS)
    spend = sum(out[p]['spend'] for p in PORTALS)
    out['ALL'] = {'rev': rev, 'spend': spend,
                  'orders': sum(out[p]['orders'] for p in PORTALS),
                  'roas': round(rev / spend, 2) if spend else 0.0}
    return out


def ensure(store: dict, ntn_db: str, token: str, now=None, days: int = 8) -> tuple[dict, bool]:
    """Freeze any completed day in the last `days` that isn't stored yet.

    A day is frozen only once it has settled: strictly before today, and — for
    the immediately-previous day — only after 01:00 IST, so overnight orders and
    Meta attribution are in. Returns (store, changed).
    """
    now = now or datetime.now(IST)
    today = now.strftime('%Y-%m-%d')
    yesterday = (now - timedelta(days=1)).strftime('%Y-%m-%d')
    accounts = _configured_accounts()
    changed = False
    for i in range(1, days + 1):
        d = (now - timedelta(days=i)).strftime('%Y-%m-%d')
        if d in store:
            continue
        if d >= today:
            continue
        if d == yesterday and now.hour < 1:
            continue                    # let the previous day settle past 01:00 IST
        try:
            spend = meta_daily_spend(d, token, accounts)
        except Exception:
            print(f'  daily_finals: skipping {d} — Meta pull incomplete, will retry next run')
            continue
        shop = shopify_daily(ntn_db, d)
        day_spend = sum(spend.values())
        day_sales = sum(s['sales'] for s in shop.values())
        if day_spend == 0 and day_sales == 0:
            continue                    # nothing ran that day; don't freeze an empty
        # Guard against a Shopify INGEST GAP freezing a wrong number forever. A
        # day with real ad spend but zero sales is never genuine — it means the
        # orders haven't been ingested yet (this is exactly how 22 Jul nearly
        # froze at Rs0 / ROAS 0.00 while the ingest was down). Skip and retry;
        # a frozen day is permanent, so better late than wrong.
        if day_spend > 0 and day_sales == 0:
            print(f'  daily_finals: NOT freezing {d} — Rs{day_spend:,.0f} spend but Rs0 sales '
                  f'(Shopify ingest gap); will retry once orders land')
            continue
        store[d] = {p: {'sales': shop[p]['sales'], 'spend': round(spend[p], 2),
                        'orders': shop[p]['orders']} for p in PORTALS}
        changed = True
        tot_s = sum(v['sales'] for v in store[d].values())
        tot_sp = sum(v['spend'] for v in store[d].values())
        print(f'  daily_finals: froze {d} — sales Rs{tot_s:,.0f} / spend Rs{tot_sp:,.0f} '
              f'= {tot_s / tot_sp if tot_sp else 0:.2f}')
    return store, changed


if __name__ == '__main__':
    import argparse
    import os
    ap = argparse.ArgumentParser()
    ap.add_argument('--store', default='state/daily_finals.json')
    ap.add_argument('--ntn-db', default='state/ntn.db')
    ap.add_argument('--days', type=int, default=8)
    args = ap.parse_args()
    st = load(args.store)
    st, changed = ensure(st, args.ntn_db, os.environ['META_ACCESS_TOKEN'], days=args.days)
    if changed:
        Path(args.store).parent.mkdir(parents=True, exist_ok=True)
        Path(args.store).write_text(json.dumps(st, indent=1, sort_keys=True))
        print(f'wrote {args.store} ({len(st)} days)')
    else:
        print('no new days to freeze')
