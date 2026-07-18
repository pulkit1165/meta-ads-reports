#!/usr/bin/env python3
"""
roas_email.py — the 40-minute ROAS email: blended portal ROAS + camp closing,
in one HTML mail.

Runs every 40 minutes (`.github/workflows/roas-email.yml`), read-only against
both databases. It never pauses anything — the actuator lives in
camp_closing.py and is armed separately.

Sections:
  1. Blended ROAS per website — Shopify sale / Meta spend / products live,
     with the change since the previous hour.
  2. Camp closing — only campaigns needing a decision (PAUSE / REVIEW / WATCH).
     Silent when there's nothing to act on, so the mail stays skimmable.

Products are counted PER WEBSITE: the same product live on SM and on SML is two
listings being advertised, and the ALL row sums rather than deduplicates.

Usage:
  GMAIL_USER=... GMAIL_APP_PASSWORD=... python3 scripts/v2/roas_email.py \
      --snap-db state/camp_snapshots.db --ntn-db state/ntn.db
  ...  --dry-run     # print the mail, send nothing
"""
from __future__ import annotations

import argparse
import json
import os
import smtplib
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from portal_hourly import (  # noqa: E402
    PORTALS, all_portal_rows, build_rows, summarise, today_ist,
)
from success_lookup import TARGET_ROAS, build_both  # noqa: E402
from camp_closing import build_first_activity, collect  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))

RECIPIENTS = [
    'pulkitsharma1165@gmail.com',
    's.harpreetsahota@gmail.com',
    'yadavjagdeep.studdmuffyn@gmail.com',
    'navdeep.studdmuffyn@gmail.com',
    'kindattitude@gmail.com',
]

WEBSITE = {'SM': 'Studd Muffyn', 'SML': 'SM Life', 'NBP': 'Nuskhe by Paras'}

CSS = """
body{font-family:-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#1b2733;
     margin:0;padding:0;background:#eef1f6}
.wrap{max-width:680px;margin:0 auto;padding:20px 14px 34px}
.card{background:#fff;border-radius:10px;padding:20px 22px;margin-bottom:14px;
      box-shadow:0 1px 3px rgba(16,32,56,.09)}
.hero{text-align:center;padding:26px 22px 22px}
.roas{font-size:46px;font-weight:700;color:#12355b;line-height:1}
.roas span{font-size:17px;font-weight:600;color:#7a8798;margin-left:6px}
.sub{font-size:13px;color:#5a6b7d;margin-top:9px}
.vs{font-size:12px;color:#8a97a5;margin-top:7px}
h2{font-size:12px;margin:0 0 12px;color:#7a8798;font-weight:700;
   text-transform:uppercase;letter-spacing:.07em}
table{border-collapse:collapse;width:100%;font-size:14px}
th{color:#8a97a5;text-align:right;padding:0 0 8px;font-weight:600;font-size:11px;
   text-transform:uppercase;letter-spacing:.05em;border-bottom:1px solid #e6ecf3}
th:first-child,td:first-child{text-align:left}
td{padding:10px 0;border-bottom:1px solid #f0f3f7;text-align:right}
tr:last-child td{border-bottom:none}
.site{font-weight:600;color:#1b2733}
.tot td{font-weight:700;color:#12355b;border-top:2px solid #dde4ee;border-bottom:none;padding-top:11px}
.up{color:#0a7d3c;font-weight:600}.dn{color:#c0392b;font-weight:600}.mut{color:#aab4c0}
.big{font-weight:700;font-size:15px}
.act{padding:11px 0;border-bottom:1px solid #f0f3f7}
.act:last-child{border-bottom:none}
.act .nm{font-size:13px;color:#1b2733;font-weight:600}
.act .dt{font-size:12px;color:#7a8798;margin-top:3px}
.tag{display:inline-block;font-size:10px;font-weight:700;padding:2px 7px;border-radius:3px;
     letter-spacing:.05em;vertical-align:1px;margin-right:7px}
.t-pause{background:#fdeae7;color:#b03024}
.t-review{background:#fff2d4;color:#8a6100}
.t-watch{background:#eef3f9;color:#4a6580}
.foot{font-size:11px;color:#94a0ad;text-align:center;line-height:1.7;padding:0 8px}
.ok{font-size:13px;color:#0a7d3c;font-weight:600}
"""


def _pct(cur, prev):
    if prev in (None, 0):
        return ''
    d = (cur - prev) / prev * 100
    cls = 'up' if d > 0 else 'dn' if d < 0 else 'mut'
    return f'<span class="{cls}">{d:+.0f}%</span>'


def build_html(day, rows, tot, closing, slot, lk16, yday=None, yday_date='', gap_note=''):
    latest = max((r['slot'] for r in rows if r['has_snap'] and r['ad_spend']), default=None)
    prev = max((r['slot'] for r in rows
                if r['has_snap'] and r['ad_spend'] and r['slot'] < (latest or '')), default=None)

    def at(portal, s):
        return next((r for r in rows if r['portal'] == portal and r['slot'] == s), None)

    a = tot['ALL']
    h = [f'<style>{CSS}</style><div class="wrap">']

    # ---- hero: the one number that matters ----
    h.append('<div class="card hero">')
    h.append(f'<div class="roas">{a["roas"]:.2f}<span>blended ROAS</span></div>')
    h.append(f'<div class="sub">&#8377;{a["rev"]:,.0f} sales on &#8377;{a["spend"]:,.0f} '
             f'spend &nbsp;&middot;&nbsp; {a["orders"]:,} orders &nbsp;&middot;&nbsp; '
             f'{a["products"]} products live</div>')
    if yday:
        y = yday['ALL']
        d = a['roas'] - y['roas']
        cls = 'up' if d > 0 else 'dn' if d < 0 else 'mut'
        h.append(f'<div class="vs">vs yesterday {y["roas"]:.2f} '
                 f'<span class="{cls}">{d:+.2f}</span></div>')
    h.append(f'<div class="vs">{day} &middot; as of {slot[-5:]} IST</div>')
    h.append('</div>')

    # ---- by website ----
    h.append('<div class="card"><h2>Today by website</h2><table>')
    h.append('<tr><th>Website</th><th>Sales</th><th>Spend</th><th>ROAS</th><th>Products</th></tr>')
    for p in PORTALS:
        t = tot[p]
        yv = (f'<div class="mut" style="font-size:11px">yest {yday[p]["roas"]:.2f}</div>'
              if yday else '')
        h.append(f'<tr><td class="site">{WEBSITE[p]}</td>'
                 f'<td>&#8377;{t["rev"]:,.0f}</td><td>&#8377;{t["spend"]:,.0f}</td>'
                 f'<td class="big">{t["roas"]:.2f}{yv}</td><td>{t["products"]}</td></tr>')
    h.append(f'<tr class="tot"><td>All</td><td>&#8377;{a["rev"]:,.0f}</td>'
             f'<td>&#8377;{a["spend"]:,.0f}</td><td>{a["roas"]:.2f}</td>'
             f'<td>{a["products"]}</td></tr>')
    h.append('</table></div>')

    # ---- latest hour ----
    if latest:
        h.append(f'<div class="card"><h2>Latest hour &mdash; {latest[-5:]} IST</h2><table>')
        h.append('<tr><th>Website</th><th>Sales</th><th>Spend</th><th>ROAS</th>'
                 '<th>Products</th></tr>')
        for p in PORTALS:
            c = at(p, latest)
            if not c:
                continue
            pv = at(p, prev) if prev else None
            dp = ''
            if pv is not None:
                d = c['products'] - pv['products']
                dp = (f' <span class="{"dn" if d < 0 else "up"}">{d:+d}</span>' if d
                      else ' <span class="mut">&mdash;</span>')
            h.append(f'<tr><td class="site">{WEBSITE[p]}</td>'
                     f'<td>&#8377;{c["shopify_sale"]:,.0f}</td>'
                     f'<td>&#8377;{c["ad_spend"]:,.0f}</td>'
                     f'<td class="big">{c["roas"]:.2f}</td>'
                     f'<td>{c["products"]}{dp}</td></tr>')
        h.append('</table></div>')

    # ---- decisions ----
    act = [r for r in closing if r['verdict'] in
           ('PAUSE', 'PAUSE (not whitelisted)', 'REVIEW', 'WATCH')]
    h.append('<div class="card">')
    h.append(f'<h2>Needs a decision &mdash; {len(act)}</h2>')
    if not act:
        h.append('<div class="ok">&#10003; Nothing over-spending below target.</div>')
    else:
        for r in act[:12]:
            v = r['verdict']
            cls = ('t-pause' if v.startswith('PAUSE')
                   else 't-review' if v == 'REVIEW' else 't-watch')
            sr = (f", {r['success_rate']}% recover" if r['success_rate'] is not None else '')
            mg = (f", 3h {r['marginal_3h']:.2f}" if r['marginal_3h'] is not None else '')
            h.append(f'<div class="act"><div class="nm">'
                     f'<span class="tag {cls}">{v.split(" ")[0]}</span>'
                     f'{r["campaign_name"][:54]}</div>'
                     f'<div class="dt">{r["portal"]} &middot; {r["spend_pct"]:.0f}% of budget '
                     f'&middot; ROAS {r["roas"]:.2f}{mg}{sr}</div></div>')
        if len(act) > 12:
            h.append(f'<div class="dt" style="padding-top:9px">+{len(act) - 12} more '
                     f'in the Camp Closing sheet</div>')
    h.append('</div>')

    n_scale = sum(1 for r in closing if r['verdict'] == 'SCALE')
    foot = [f'{len(closing)} campaigns tracked &middot; {n_scale} scale candidates '
            f'(ROAS &ge; 2.0 past half budget)',
            'Blended ROAS counts <b>all</b> Shopify revenue, including organic and repeat &mdash; '
            'a profitability read per website, not a campaign metric.',
            'Campaign figures are Meta pixel-attributed. Nothing is paused automatically.']
    if gap_note:
        foot.insert(0, gap_note)
    h.append('<div class="foot">' + '<br>'.join(foot) + '</div>')
    h.append('</div>')
    return '\n'.join(h)


def text_fallback(day, tot, closing, slot):
    a = tot['ALL']
    out = [f'BLENDED ROAS {a["roas"]:.2f} — {day} as of {slot[-5:]} IST',
           f'Rs{a["rev"]:,.0f} sales / Rs{a["spend"]:,.0f} spend · {a["products"]} products live', '']
    for p in PORTALS:
        t = tot[p]
        out.append(f'  {WEBSITE[p]:16} Rs{t["rev"]:>9,.0f} / Rs{t["spend"]:>9,.0f} = '
                   f'{t["roas"]:.2f}  ({t["products"]} products)')
    act = [r for r in closing if r['verdict'] in ('PAUSE', 'REVIEW', 'WATCH')]
    out += ['', f'NEEDS A DECISION: {len(act)}']
    for r in act[:15]:
        sr = f"{r['success_rate']}% recover" if r['success_rate'] is not None else 'no history'
        out.append(f'  [{r["verdict"]}] {r["portal"]} {r["campaign_name"][:46]} — '
                   f'{r["spend_pct"]:.0f}% spent, ROAS {r["roas"]:.2f}, {sr}')
    return '\n'.join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--snap-db', default='state/camp_snapshots.db')
    ap.add_argument('--ntn-db', default='state/ntn.db')
    ap.add_argument('--day', default=None)
    ap.add_argument('--to', default=None, help='comma-separated override of RECIPIENTS')
    ap.add_argument('--dry-run', action='store_true', help='print, do not send')
    ap.add_argument('--min-spend', type=float, default=2000,
                    help='skip sending below this much ad spend today (default 2000)')
    ap.add_argument('--state', default=None,
                    help='JSON file tracking which IST hours have been emailed')
    ap.add_argument('--force', action='store_true',
                    help='send even if this hour was already emailed')
    args = ap.parse_args()

    # ── once-per-hour gate ────────────────────────────────────────────────
    # GitHub skips roughly a third of cron ticks, so the workflow fires three
    # times an hour and this gate keeps exactly one mail per IST hour. Missing
    # two of three attempts still gets the hour delivered.
    now = datetime.now(IST)
    this_hour = now.strftime('%Y-%m-%d %H:00')
    state, sent = None, []
    if args.state:
        state = Path(args.state)
        if state.exists():
            try:
                sent = json.loads(state.read_text()).get('sent_hours', [])
            except Exception:
                sent = []
        if this_hour in sent and not args.force:
            print(f'{this_hour} IST already emailed — skipping (use --force to override)')
            return

    day = args.day or today_ist()
    for p in (args.snap_db, args.ntn_db):
        if not Path(p).exists():
            print(f'FATAL: missing DB {p}'); sys.exit(1)

    portal_rows = build_rows(args.snap_db, args.ntn_db, day)
    if not portal_rows:
        print(f'no data for {day} — not sending'); return
    tot = summarise(portal_rows)
    rows = portal_rows + all_portal_rows(portal_rows)

    # Just after IST midnight the day has almost no spend, so ROAS reads 0.00 and
    # the mail is pure noise — three of them land before 01:30 every night.
    if tot['ALL']['spend'] < args.min_spend:
        print(f"today's spend Rs{tot['ALL']['spend']:,.0f} is below the "
              f"Rs{args.min_spend:,.0f} floor — the day has barely started, not sending")
        return

    # If earlier hours never went out (GitHub skipped all three attempts, or the
    # run failed), say so rather than letting the reader assume continuous cover.
    gap_note = ''
    todays_sent = [h for h in sent if h.startswith(day)]
    if todays_sent:
        last = max(todays_sent)
        missed = int(this_hour[11:13]) - int(last[11:13]) - 1
        if missed > 0:
            gap_note = (f'&#9888; No email went out for the previous '
                        f'{missed} hour{"s" if missed > 1 else ""} '
                        f'(last was {last[11:16]} IST).')

    yday_date = (datetime.strptime(day, '%Y-%m-%d') - timedelta(days=1)).strftime('%Y-%m-%d')
    try:
        yrows = build_rows(args.snap_db, args.ntn_db, yday_date)
        yday = summarise(yrows) if yrows else None
    except Exception:
        yday = None

    con = sqlite3.connect(f'file:{args.snap_db}?mode=ro', uri=True)
    lk16, lk21 = build_both(args.snap_db, exclude_day=day)
    closing = collect(con, day, lk16, lk21, build_first_activity(con))
    con.close()
    slot = closing[0]['slot'] if closing else f'{day} 00:00'

    html = build_html(day, rows, tot, closing, slot, lk16, yday, yday_date, gap_note)
    text = text_fallback(day, tot, closing, slot)
    a = tot['ALL']
    n_act = sum(1 for r in closing if r['verdict'] in ('PAUSE', 'REVIEW', 'WATCH'))
    subject = (f'ROAS {a["roas"]:.2f} · ₹{a["rev"]/100000:.1f}L sales / ₹{a["spend"]/100000:.1f}L '
               f'spend · {a["products"]} products'
               + (f' · {n_act} need action' if n_act else '') + f' · {slot[-5:]} IST')

    to = [x.strip() for x in args.to.split(',')] if args.to else RECIPIENTS
    user = os.environ.get('GMAIL_USER')
    pw = os.environ.get('GMAIL_APP_PASSWORD')

    print(text)
    print(f'\nsubject: {subject}')
    if args.dry_run or not (user and pw):
        print(f"[not sent] {'dry-run' if args.dry_run else 'GMAIL_USER/GMAIL_APP_PASSWORD unset'}"
              f" — would go to {len(to)} recipients")
        Path('out').mkdir(exist_ok=True)
        Path('out/roas_email_preview.html').write_text(html)
        print('preview: out/roas_email_preview.html')
        return

    msg = MIMEMultipart('alternative')
    msg['Subject'] = subject
    msg['From'] = user
    msg['To'] = ', '.join(to)
    msg.attach(MIMEText(text, 'plain'))
    msg.attach(MIMEText(html, 'html'))
    with smtplib.SMTP('smtp.gmail.com', 587, timeout=45) as s:
        s.starttls()
        s.login(user, pw)
        s.sendmail(user, to, msg.as_string())
    print(f'sent to {len(to)} recipients: {", ".join(to)}')

    if state is not None:
        sent.append(this_hour)
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(json.dumps({'sent_hours': sorted(set(sent))[-72:]}, indent=1))
        print(f'recorded {this_hour} in {state}')


if __name__ == '__main__':
    main()
