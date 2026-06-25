#!/usr/bin/env python3
"""
camp_alerts.py — Meta Ads automated email-alert engine (runs every 15 min).

Conditions:
  NEW    (age < 24h):  spend >= 30% of daily budget AND roas in
                       {0, <0.5, <0.75, <1.0, <1.25, <1.5}
  MATURE (age >= 24h): spend >= 50% of daily budget AND roas in {<1.25, <1.60}

Dedup: one alert per ROAS bucket per campaign per day; re-trigger only when the
campaign drops into a LOWER (more severe) bucket than already alerted today.

Each email includes current ROAS, ROAS 1h ago (from snapshots), change %, trend,
campaign id, ad-account id, objective, and the Ads Manager link.

Email is sent via Gmail SMTP using env GMAIL_USER + GMAIL_APP_PASSWORD. If those
are unset the engine runs fully but only PRINTS (dormant) — no send, no log-as-sent.

Usage:
  META_ACCESS_TOKEN=... GMAIL_USER=... GMAIL_APP_PASSWORD=... \
    python3 scripts/v2/camp_alerts.py --db state/camp_snapshots.db
  add --dry-run to evaluate + print without sending or logging.
"""
import argparse
import os
import smtplib
import sqlite3
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText

from camp_live import fetch_active_campaigns

IST = timezone(timedelta(hours=5, minutes=30))

RECIPIENTS = [
    's.harpreetsahota@gmail.com', 'jagdeep.studdmuffyn@gmail.com',
    'navdeep.studdmuffyn@gmail.com', 'pulkit.studdmuffyn@gmail.com',
    'kindattitude@gmail.com', 'ankur.bhati2204@gmail.com',
]

NEW_BUCKETS = [0.5, 0.75, 1.0, 1.25, 1.5]      # plus the special 0.0 (roas==0)
MATURE_BUCKETS = [1.25, 1.60]


def bucket_for(roas, is_new):
    """Return the most-severe bucket threshold the roas falls into, or None."""
    if is_new:
        if roas == 0:
            return 0.0
        for t in NEW_BUCKETS:
            if roas < t:
                return t
        return None
    for t in MATURE_BUCKETS:
        if roas < t:
            return t
    return None


def prev_roas(con, campaign_id, now):
    slot = (now - timedelta(hours=1)).strftime('%Y-%m-%d %H:00')
    r = con.execute("SELECT roas FROM campaign_hourly_snapshots "
                    "WHERE campaign_id=? AND hour_slot=?", (campaign_id, slot)).fetchone()
    return r[0] if r else None


def min_bucket_today(con, campaign_id, day):
    r = con.execute("SELECT MIN(bucket) FROM camp_alert_log WHERE campaign_id=? AND day=?",
                    (campaign_id, day)).fetchone()
    return r[0] if r and r[0] is not None else None


def ads_manager_url(account_id, campaign_id):
    aid = account_id.replace('act_', '')
    return (f"https://adsmanager.facebook.com/adsmanager/manage/campaigns?"
            f"act={aid}&selected_campaign_ids={campaign_id}")


def build_email(c, spend_pct, pr, reason, now):
    roas = c['roas']
    if pr is None:
        chg = "n/a (no prior snapshot)"
        trend = "New / no baseline"
    else:
        d = ((roas - pr) / pr * 100) if pr else 0.0
        chg = f"{d:+.2f}%"
        trend = "Declining" if d < -1 else "Improving" if d > 1 else "Stable"
    subject = f"[Meta Alert] {c['campaign_name']} | Spend {spend_pct:.0f}% | ROAS {roas:.2f}"
    body = f"""Campaign Name: {c['campaign_name']}
Campaign ID: {c['campaign_id']}
Ad Account ID: {c['account_id']}  ({c['account_name']})
Objective: {c['objective']}
Campaign Age: {c['age_hours']} hours

Daily Budget: Rs {c['daily_budget']:,.0f}
Spend: Rs {c['spend']:,.0f}
Spend %: {spend_pct:.1f}%
Revenue: Rs {c['revenue']:,.0f}
Orders: {c['orders']}

Current ROAS: {roas:.2f}
ROAS 1 Hour Ago: {('%.2f' % pr) if pr is not None else 'n/a'}
ROAS Change: {chg}
Trend: {trend}

Alert Trigger:
{reason}

Campaign Link:
{ads_manager_url(c['account_id'], c['campaign_id'])}

Timestamp:
{now.strftime('%Y-%m-%d %H:%M:%S IST')}

Recommended Action:
Review campaign performance and take scaling, optimization, or shutdown decisions as required.
"""
    return subject, body


def send_email(subject, body, user, pw):
    msg = MIMEText(body)
    msg['Subject'] = subject
    msg['From'] = user
    msg['To'] = ', '.join(RECIPIENTS)
    with smtplib.SMTP('smtp.gmail.com', 587, timeout=30) as s:
        s.starttls()
        s.login(user, pw)
        s.sendmail(user, RECIPIENTS, msg.as_string())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', default='state/camp_snapshots.db')
    ap.add_argument('--accounts', nargs='*', default=None)
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()
    tok = os.environ['META_ACCESS_TOKEN']
    user = os.environ.get('GMAIL_USER')
    pw = os.environ.get('GMAIL_APP_PASSWORD')
    can_send = bool(user and pw) and not args.dry_run

    now = datetime.now(IST)
    day = now.strftime('%Y-%m-%d')
    con = sqlite3.connect(args.db)
    con.executescript("""CREATE TABLE IF NOT EXISTS camp_alert_log (
      campaign_id TEXT, day TEXT, bucket REAL, sent_ts TEXT, roas REAL, spend_pct REAL,
      PRIMARY KEY (campaign_id, day, bucket));""")

    rows = fetch_active_campaigns(tok, args.accounts, now=now)
    fired = 0
    for c in rows:
        if c.get('status') != 'Active':
            continue  # don't alert on campaigns already paused (even if they spent today)
        if not c['daily_budget']:
            continue
        spend_pct = c['spend'] / c['daily_budget'] * 100
        is_new = (c['age_hours'] is not None and c['age_hours'] < 24)
        thr = 30 if is_new else 50
        if spend_pct < thr:
            continue
        bucket = bucket_for(c['roas'], is_new)
        if bucket is None:
            continue
        prior = min_bucket_today(con, c['campaign_id'], day)
        if prior is not None and bucket >= prior:
            continue  # already alerted this/an equal-or-worse bucket today
        # build reason
        kind = "New Campaign Alert" if is_new else "Mature Campaign Alert"
        thresh_txt = "1.00" if (is_new and bucket == 1.0) else (f"{bucket:.2f}" if bucket else "0")
        reason = (f"{kind} - Campaign has spent {spend_pct:.0f}% of budget and is "
                  f"{'at 0 ROAS' if bucket == 0 else f'below {bucket:.2f} ROAS'}.")
        pr = prev_roas(con, c['campaign_id'], now)
        subject, body = build_email(c, spend_pct, pr, reason, now)
        fired += 1
        if can_send:
            try:
                send_email(subject, body, user, pw)
                con.execute("INSERT OR REPLACE INTO camp_alert_log VALUES (?,?,?,?,?,?)",
                            (c['campaign_id'], day, bucket, now.isoformat(timespec='seconds'),
                             c['roas'], round(spend_pct, 1)))
                con.commit()
                print(f"SENT  {subject}")
            except Exception as e:
                print(f"FAIL  {subject}  ({e})")
        else:
            tag = "DRY" if args.dry_run else "DORMANT(no GMAIL creds)"
            print(f"[{tag}] {subject}  | age={c['age_hours']}h pct={spend_pct:.0f}% bucket={bucket}")
    con.close()
    print(f"evaluated {len(rows)} active campaigns -> {fired} alert(s) "
          f"{'sent' if can_send else 'matched (not sent)'}")


if __name__ == '__main__':
    main()
