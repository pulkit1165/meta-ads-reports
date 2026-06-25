#!/usr/bin/env python3
"""
push_live_roas_to_sheet.py — write the live ROAS tracker into a Google Sheet tab.

Reads camp_snapshots.db (latest hour) + computes 1h/3h/day-start deltas, then
writes the "Hourly ROAS Tracker" view into a tab of an EXISTING sheet via the
gspread service account (antriksh-bot@). Tab is created if missing, then fully
overwritten each run (so it's safe to call hourly from the workflow).

All numbers are Meta pixel-attributed.

Usage:
  python3 scripts/v2/push_live_roas_to_sheet.py --db state/camp_snapshots.db \
      --sheet 1eW2_qPdsKJ8zAV5-hsXA5HtfVH9NwDhQLyHYGKz5hXk --tab "Live ROAS Tracker"
  (auth: GOOGLE_SERVICE_ACCOUNT_FILE env, or --sa <path>)
"""
import argparse
import os
import sqlite3
from datetime import datetime, timezone, timedelta

import gspread

IST = timezone(timedelta(hours=5, minutes=30))


def bucket_for(roas, is_new):
    if is_new:
        if roas == 0:
            return 0.0
        for t in (0.5, 0.75, 1.0, 1.25, 1.5):
            if roas < t:
                return t
        return None
    for t in (1.25, 1.60):
        if roas < t:
            return t
    return None


def pct(a, b):
    if a is None or b in (None, 0):
        return None
    return (a - b) / b * 100


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', default='state/camp_snapshots.db')
    ap.add_argument('--sheet', required=True)
    ap.add_argument('--tab', default='Live ROAS Tracker')
    ap.add_argument('--sa', default=os.environ.get('GOOGLE_SERVICE_ACCOUNT_FILE',
                                                   'google-service-account.json'))
    args = ap.parse_args()

    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row
    latest = con.execute("SELECT MAX(hour_slot) FROM campaign_hourly_snapshots").fetchone()[0]
    if not latest:
        print("no snapshots yet — nothing to write")
        return
    ls = datetime.strptime(latest, '%Y-%m-%d %H:00').replace(tzinfo=IST)
    s1 = (ls - timedelta(hours=1)).strftime('%Y-%m-%d %H:00')
    s3 = (ls - timedelta(hours=3)).strftime('%Y-%m-%d %H:00')
    today = ls.strftime('%Y-%m-%d')

    def roas_at(cid, slot):
        r = con.execute("SELECT roas FROM campaign_hourly_snapshots WHERE campaign_id=? AND hour_slot=?",
                        (cid, slot)).fetchone()
        return r[0] if r else None

    cur = con.execute("SELECT * FROM campaign_hourly_snapshots WHERE hour_slot=?", (latest,)).fetchall()
    rows = []
    for c in cur:
        cid = c['campaign_id']
        r1, r3 = roas_at(cid, s1), roas_at(cid, s3)
        ds = con.execute("SELECT roas FROM campaign_hourly_snapshots WHERE campaign_id=? AND hour_slot LIKE ? "
                         "ORDER BY hour_slot LIMIT 1", (cid, today + '%')).fetchone()
        ds = ds[0] if ds else None
        is_new = (c['age_hours'] is not None and c['age_hours'] < 24)
        spend_pct = (c['spend'] / c['daily_budget'] * 100) if c['daily_budget'] else 0
        bkt = bucket_for(c['roas'], is_new)
        alerting = bool(bkt is not None and spend_pct >= (30 if is_new else 50))
        d1 = pct(c['roas'], r1)
        trend = 'Improving' if (d1 and d1 > 1) else 'Declining' if (d1 and d1 < -1) else 'Stable'
        status = c['status'] if 'status' in c.keys() and c['status'] else 'Active'
        rows.append([
            c['campaign_name'], c['account_name'], status, c['objective'],
            c['age_hours'], round(c['daily_budget']), round(c['spend']),
            round(spend_pct, 1), round(c['roas'], 2),
            round(d1, 1) if d1 is not None else '', round(pct(c['roas'], r3), 1) if pct(c['roas'], r3) is not None else '',
            round(ds, 2) if ds is not None else '', round(c['revenue']), c['orders'],
            round(c['ctr'], 2), round(c['cpc'], 1), round(c['cpm'], 1), round(c['cpa']) if c['cpa'] else '',
            trend, 'YES' if alerting else '',
            ('New' if is_new else 'Mature') + ': ' + (('ROAS=0' if bkt == 0 else 'ROAS<%.2f' % bkt) if alerting else '')
            if alerting else '',
        ])
    con.close()
    rows.sort(key=lambda r: -(r[6] or 0))   # by Spend

    header = ['Campaign', 'Account', 'Status', 'Objective', 'Age(h)', 'Budget ₹', 'Spend ₹', 'Spend %',
              'ROAS', 'Δ1h %', 'Δ3h %', 'DayStart ROAS', 'Revenue ₹', 'Orders',
              'CTR %', 'CPC ₹', 'CPM ₹', 'CPA ₹', 'Trend', 'Alert?', 'Alert Reason']
    updated = datetime.now(IST).strftime('%d %b %Y, %H:%M IST')
    n_alert = sum(1 for r in rows if r[19] == 'YES')
    n_paused = sum(1 for r in rows if r[2] == 'Paused')
    top = [[f"📡 LIVE ROAS TRACKER — updated {updated} · {len(rows)} campaigns today "
            f"({len(rows) - n_paused} active, {n_paused} paused) · {n_alert} alerting · Meta pixel · auto-refresh hourly"]]
    values = top + [header] + rows

    gc = gspread.service_account(filename=args.sa)
    sh = gc.open_by_key(args.sheet)
    try:
        ws = sh.worksheet(args.tab)
        ws.clear()
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=args.tab, rows=max(len(values) + 20, 100), cols=len(header))
    ws.update(range_name='A1', values=values)
    # light formatting: bold header rows, freeze
    try:
        ws.freeze(rows=2)
        ws.format('A1:U2', {'textFormat': {'bold': True}})
    except Exception:
        pass
    print(f"wrote {len(rows)} campaigns to '{args.tab}' (slot {latest}, {n_alert} alerting)")


if __name__ == '__main__':
    main()
