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
from pathlib import Path

import re

from camp_live import ACCOUNT_ERRORS, fetch_active_campaigns

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def configured_accounts():
    """Ad account ids from config/accounts.env.

    Preferred over me/adaccounts discovery for two reasons. First, discovery is
    broken for this business: Meta returns HTTP 500 paginating the account list
    (75 accounts), and the first page alone silently truncates it. Second,
    discovery only ever listed accounts the token had a role on — which is how
    NBP Skin, ~Rs1L/day of spend, stayed invisible for weeks.

    The configured list is the same one check_account_access.py audits, so a
    blocked account is a loud failure rather than a silent omission.
    """
    out = []
    for name in ('config/accounts.env', '.env'):
        p = REPO_ROOT / name
        if not p.exists():
            continue
        for line in p.read_text(errors='ignore').splitlines():
            m = re.match(r'^\s*[A-Z][A-Z0-9_]*\s*=\s*(act_\d+)\s*$', line.strip())
            if m and m.group(1) not in out:
                out.append(m.group(1))
    return out

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
  status        TEXT,            -- Active / Paused (delivered today)
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
        'objective', 'status', 'created_time', 'age_hours', 'daily_budget', 'spend', 'revenue',
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

    accounts = args.accounts or configured_accounts() or None
    print(f"accounts: {len(accounts) if accounts else 0} from config"
          if accounts else "accounts: falling back to me/adaccounts discovery")
    rows = fetch_active_campaigns(tok, accounts, now=now)

    # A PARTIAL snapshot is worse than no snapshot. If some accounts errored
    # transiently, the hour would be written looking complete while missing
    # their spend — downstream everything (blended ROAS, budgets, the closing
    # report) would treat the deflated number as authoritative. That is exactly
    # how NBP Skin cost ~Rs1L/day undetected. Permission errors are excluded
    # from this check: they are permanent and already known, so blocking on them
    # would mean never writing a snapshot at all.
    transient = [e for e in ACCOUNT_ERRORS if 'NOT grant' not in e[2]]
    if transient:
        print(f"ABORTING WRITE: {len(transient)} account(s) failed transiently — "
              f"refusing to save a partial hour that would understate spend:")
        for aid, name, err in transient:
            print(f"  - {aid} ({name}): {err[:110]}")
        print("previous snapshot left in place; the next run retries")
        raise SystemExit(1)

    con = sqlite3.connect(args.db)
    con.executescript(SCHEMA)
    # add status column to a pre-existing table (idempotent migration)
    cols = {r[1] for r in con.execute("PRAGMA table_info(campaign_hourly_snapshots)")}
    if 'status' not in cols:
        con.execute("ALTER TABLE campaign_hourly_snapshots ADD COLUMN status TEXT")
    payload = [(ts, hour_slot, r['account_id'], r['account_name'], r['campaign_id'],
                r['campaign_name'], r['objective'], r['status'], r['created_time'], r['age_hours'],
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
    if ACCOUNT_ERRORS:
        # Loud, because a skipped account silently understates every downstream
        # number — spend, blended ROAS, and the closing report's coverage.
        print(f"WARNING: {len(ACCOUNT_ERRORS)} ad account(s) could not be read; their "
              f"spend is MISSING from this snapshot:")
        for aid, name, err in ACCOUNT_ERRORS:
            print(f"  - {aid} ({name}): {err}")


if __name__ == '__main__':
    main()
