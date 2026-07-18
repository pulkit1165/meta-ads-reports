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
body{font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#1b2733;margin:0;padding:18px;background:#f6f8fb}
h2{font-size:16px;margin:22px 0 8px;color:#12355b}
h3{font-size:13px;margin:18px 0 6px;color:#5a6b7d;font-weight:600;text-transform:uppercase;letter-spacing:.04em}
table{border-collapse:collapse;width:100%;background:#fff;font-size:13px;margin-bottom:6px}
th{background:#1f4e78;color:#fff;text-align:right;padding:7px 9px;font-weight:600;font-size:12px}
th:first-child,td:first-child{text-align:left}
td{padding:6px 9px;border-bottom:1px solid #e6ecf3;text-align:right}
tr:nth-child(even) td{background:#f7fafd}
.tot td{font-weight:700;background:#eef3f9!important;border-top:2px solid #1f4e78}
.up{color:#0a7d3c}.dn{color:#c0392b}.mut{color:#8a97a5}
.pause td{background:#fdeae7!important}.review td{background:#fff4d6!important}.watch td{background:#fffaea!important}
.note{font-size:11px;color:#7a8798;margin:4px 0 16px;line-height:1.5}
.big{font-size:22px;font-weight:700;color:#12355b}
"""


def _pct(cur, prev):
    if prev in (None, 0):
        return ''
    d = (cur - prev) / prev * 100
    cls = 'up' if d > 0 else 'dn' if d < 0 else 'mut'
    return f'<span class="{cls}">{d:+.0f}%</span>'


def build_html(day, rows, tot, closing, slot, lk16, yday=None, yday_date=''):
    latest = max((r['slot'] for r in rows if r['has_snap'] and r['ad_spend']), default=None)
    prev = max((r['slot'] for r in rows
                if r['has_snap'] and r['ad_spend'] and r['slot'] < (latest or '')), default=None)

    def at(portal, s):
        return next((r for r in rows if r['portal'] == portal and r['slot'] == s), None)

    a = tot['ALL']
    h = [f'<style>{CSS}</style>',
         f'<div class="big">Blended ROAS {a["roas"]:.2f}</div>',
         f'<div class="note">{day} &middot; as of {slot[-5:]} IST &middot; '
         f'&#8377;{a["rev"]:,.0f} Shopify sales on &#8377;{a["spend"]:,.0f} Meta spend '
         f'&middot; {a["products"]} products live</div>']

    # ---- section 1: day so far, per website ----
    h.append('<h2>Today by website</h2>')
    h.append('<table><tr><th>Website</th><th>Shopify Sale</th><th>Ad Spend</th>'
             '<th>Blended ROAS</th><th>Orders</th><th>Products Live</th></tr>')
    for p in PORTALS:
        t = tot[p]
        h.append(f'<tr><td>{WEBSITE[p]} <span class="mut">({p})</span></td>'
                 f'<td>&#8377;{t["rev"]:,.0f}</td><td>&#8377;{t["spend"]:,.0f}</td>'
                 f'<td>{t["roas"]:.2f}</td><td>{t["orders"]:,}</td><td>{t["products"]}</td></tr>')
    h.append(f'<tr class="tot"><td>ALL</td><td>&#8377;{a["rev"]:,.0f}</td>'
             f'<td>&#8377;{a["spend"]:,.0f}</td><td>{a["roas"]:.2f}</td>'
             f'<td>{a["orders"]:,}</td><td>{a["products"]}</td></tr></table>')
    if yday:
        # Early in the IST day "today" is nearly empty, which makes the numbers
        # above look alarming out of context. Yesterday's close is the yardstick.
        y = yday['ALL']
        h.append(f'<div class="note">Yesterday ({yday_date}) closed at '
                 f'<b>{y["roas"]:.2f}</b> &mdash; &#8377;{y["rev"]:,.0f} on '
                 f'&#8377;{y["spend"]:,.0f}'
                 + ' &middot; ' + ' &middot; '.join(
                     f'{p} {yday[p]["roas"]:.2f}' for p in PORTALS) + '</div>')
    h.append('<div class="note">Blended = <b>all</b> Shopify revenue over Meta spend, so it '
             'includes organic and repeat orders &mdash; it is a profitability read for the '
             'website, not a campaign metric. Products are counted per website.</div>')

    # ---- section 2: latest hour ----
    if latest:
        h.append(f'<h2>Latest hour &mdash; {latest[-5:]} IST</h2>')
        h.append('<table><tr><th>Website</th><th>Sale</th><th>Spend</th><th>ROAS</th>'
                 '<th>Products</th><th>vs prev hour</th></tr>')
        for p in PORTALS:
            c = at(p, latest)
            if not c:
                continue
            pv = at(p, prev) if prev else None
            dp = ''
            if pv is not None:
                d = c['products'] - pv['products']
                if d:
                    dp = (f'<span class="{"dn" if d < 0 else "up"}">{d:+d} products</span>')
                else:
                    dp = '<span class="mut">no change</span>'
            h.append(f'<tr><td>{WEBSITE[p]}</td><td>&#8377;{c["shopify_sale"]:,.0f}</td>'
                     f'<td>&#8377;{c["ad_spend"]:,.0f}</td><td>{c["roas"]:.2f} '
                     f'{_pct(c["roas"], pv["roas"]) if pv else ""}</td>'
                     f'<td>{c["products"]}</td><td>{dp}</td></tr>')
        h.append('</table>')

    # ---- section 3: campaigns needing a decision ----
    act = [r for r in closing if r['verdict'] in
           ('PAUSE', 'PAUSE (not whitelisted)', 'REVIEW', 'WATCH')]
    h.append(f'<h2>Campaigns needing a decision &mdash; {len(act)}</h2>')
    if not act:
        h.append('<div class="note">Nothing over-spending below target right now.</div>')
    else:
        h.append('<table><tr><th>Campaign</th><th>Site</th><th>Spend %</th><th>ROAS</th>'
                 f'<th>Last 3h</th><th>Recover @{TARGET_ROAS}</th><th>Verdict</th></tr>')
        for r in act[:25]:
            cls = ('pause' if r['verdict'].startswith('PAUSE')
                   else 'review' if r['verdict'] == 'REVIEW' else 'watch')
            sr = f"{r['success_rate']}%" if r['success_rate'] is not None else '&mdash;'
            mg = f"{r['marginal_3h']:.2f}" if r['marginal_3h'] is not None else '&mdash;'
            h.append(f'<tr class="{cls}"><td>{r["campaign_name"][:52]}</td><td>{r["portal"]}</td>'
                     f'<td>{r["spend_pct"]:.0f}%</td><td>{r["roas"]:.2f}</td><td>{mg}</td>'
                     f'<td>{sr}</td><td><b>{r["verdict"]}</b></td></tr>')
        h.append('</table>')
        h.append('<div class="note">PAUSE = hit the kill matrix. REVIEW = matrix would keep it '
                 'but history says campaigns in this state almost never reach target. '
                 f'&ldquo;Recover&rdquo; = share of {lk16.n_camp_days} past campaign-days in the '
                 'same spend&times;ROAS state that finished at or above '
                 f'{TARGET_ROAS}. <b>Nothing is paused automatically.</b></div>')

    n_scale = sum(1 for r in closing if r['verdict'] == 'SCALE')
    h.append(f'<div class="note">{len(closing)} campaigns tracked &middot; {n_scale} at '
             f'ROAS &ge; 2.0 and past half their budget (scale candidates) &middot; '
             f'campaign figures are Meta pixel-attributed.</div>')
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
    args = ap.parse_args()

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

    html = build_html(day, rows, tot, closing, slot, lk16, yday, yday_date)
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
    print(f'sent to {len(to)} recipients')


if __name__ == '__main__':
    main()
