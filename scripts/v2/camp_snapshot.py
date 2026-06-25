#!/usr/bin/env python3
"""
camp_snapshot.py — hourly snapshot collector for live campaign performance.

Fetches every ACTIVE Meta campaign (all accounts) and writes one row per
campaign per hour into `campaign_hourly_snapshots`. Idempotent: re-running
inside the same hour upserts (PRIMARY KEY = hour_slot+campaign_id).

All per-campaign metrics are Meta PIXEL-attributed (see camp_live.py).

Usage:
  META_ACCESS_TOKEN=... python3 scripts/v2/camp_snapshot.py --db state/camp_snapshots.db
  ...                      python3 scripts/v2/camp_snapshot.py --db ... --accounts act_x act_y   # subset
"""
import argparse
import os
import sqlite3
from datetime import datetime, timezone, timedelta

from camp_live import fetch_active_campaigns

IST = timezone(timedelta(hours=5, minutes=30))

SCHEMA = """
CREATE TABLE IF NOT EXISTS campaign_hourly_snapshots (
  ts            TEXT,            -- exact run time, ISO IST
  hour_slot     TEXT,            -- 'YYYY-MM-DD HH:00' IST (dedup bucket)
  account_id    TEXT,
  account_name  TEXT,
  campaign_id   TEXT,
  campaign_name TEXT,
  objective     TEXT,
  created_time  TEXT,
  age_hours     REAL,
  daily_budget  REAL,
  spend         REAL,
  revenue       REAL,
  roas          REAL,
  orders        INTEGER,
  impressions   INTEGER,
  clicks        INTEGER,
  ctr           REAL,
  cpc           REAL,
  cpm           REAL,
  cpa           REAL,
  PRIMARY KEY (hour_slot, campaign_id)
);
CREATE INDEX IF NOT EXISTS idx_snap_camp ON campaign_hourly_snapshots(campaign_id, hour_slot);
CREATE INDEX IF NOT EXISTS idx_snap_hour ON campaign_hourly_snapshots(hour_slot);

CREATE TABLE IF NOT EXISTS camp_alert_log (
  campaign_id  TEXT,
  day          TEXT,     -- YYYY-MM-DD IST
  bucket       REAL,     -- ROAS bucket threshold alerted (lower = more severe)
  sent_ts      TEXT,
  roas         REAL,
  spend_pct    REAL,
  PRIMARY KEY (campaign_id, day, bucket)
);
"""

COLS = ['ts', 'hour_slot', 'account_id', 'account_name', 'campaign_id', 'campaign_name',
        'objective', 'created_time', 'age_hours', 'daily_budget', 'spend', 'revenue',
        'roas', 'orders', 'impressions', 'clicks', 'ctr', 'cpc', 'cpm', 'cpa']


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', default='state/camp_snapshots.db')
    ap.add_argument('--accounts', nargs='*', default=None)
    args = ap.parse_args()
    tok = os.environ['META_ACCESS_TOKEN']

    now = datetime.now(IST)
    ts = now.isoformat(timespec='seconds')
    hour_slot = now.strftime('%Y-%m-%d %H:00')

    rows = fetch_active_campaigns(tok, args.accounts, now=now)

    con = sqlite3.connect(args.db)
    con.executescript(SCHEMA)
    payload = [(ts, hour_slot, r['account_id'], r['account_name'], r['campaign_id'],
                r['campaign_name'], r['objective'], r['created_time'], r['age_hours'],
                r['daily_budget'], r['spend'], r['revenue'], r['roas'], r['orders'],
                r['impressions'], r['clicks'], r['ctr'], r['cpc'], r['cpm'], r['cpa'])
               for r in rows]
    con.executemany(
        f"INSERT OR REPLACE INTO campaign_hourly_snapshots ({','.join(COLS)}) "
        f"VALUES ({','.join('?' * len(COLS))})", payload)
    # 365-day retention: drop anything older
    cutoff = (now - timedelta(days=365)).isoformat(timespec='seconds')
    pruned = con.execute("DELETE FROM campaign_hourly_snapshots WHERE ts < ?", (cutoff,)).rowcount
    con.execute("DELETE FROM camp_alert_log WHERE day < ?", ((now - timedelta(days=365)).strftime('%Y-%m-%d'),))
    con.commit()
    if pruned:
        print(f"pruned {pruned} rows older than 365 days")
    tot = con.execute("SELECT COUNT(*) FROM campaign_hourly_snapshots").fetchone()[0]
    hrs = con.execute("SELECT COUNT(DISTINCT hour_slot) FROM campaign_hourly_snapshots").fetchone()[0]
    con.close()
    delivering = sum(1 for r in rows if r['spend'] > 0)
    print(f"[{hour_slot}] wrote {len(rows)} active campaigns ({delivering} delivering) | "
          f"DB now {tot} rows across {hrs} hourly slots")


if __name__ == '__main__':
    main()
