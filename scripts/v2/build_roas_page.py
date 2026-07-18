#!/usr/bin/env python3
"""
build_roas_page.py — the hourly ROAS dashboard, deployed to Vercel.

Same numbers as the hourly email, plus an hour-by-hour log for today and a
rolling archive of previous days.

The hourly log is RECONSTRUCTED from campaign_hourly_snapshots + shopify_orders
on every build rather than appended to. That means it is self-healing: an hour
the page build missed still appears the next time the page is built, as long as
the snapshot exists. Appending would have left permanent holes whenever a
deploy failed.

Usage:
  python3 scripts/v2/build_roas_page.py --snap-db state/camp_snapshots.db \
      --ntn-db state/ntn.db --out roas-live/index.html --days 7
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from portal_hourly import (  # noqa: E402
    PORTALS, all_portal_rows, build_rows, summarise,
)
from success_lookup import TARGET_ROAS, build_both  # noqa: E402
from camp_closing import build_first_activity, collect  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
WEBSITE = {'SM': 'Studd Muffyn', 'SML': 'SM Life', 'NBP': 'Nuskhe by Paras'}

CSS = """
*{box-sizing:border-box}
body{font-family:-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#1b2733;
     margin:0;background:#eef1f6}
.wrap{max-width:1040px;margin:0 auto;padding:18px 14px 50px}
.bar{display:flex;flex-wrap:wrap;align-items:baseline;gap:10px;justify-content:space-between;
     padding:4px 4px 16px}
.bar h1{font-size:17px;margin:0;color:#12355b;font-weight:700}
.stamp{font-size:12px;color:#64748b}
.stamp b{color:#1b2733}
.dot{display:inline-block;width:7px;height:7px;border-radius:50%;background:#0a7d3c;margin-right:5px}
.card{background:#fff;border-radius:10px;padding:18px 20px;margin-bottom:14px;
      box-shadow:0 1px 3px rgba(16,32,56,.09)}
.hero{text-align:center;padding:24px 20px 20px}
.roas{font-size:46px;font-weight:700;color:#12355b;line-height:1}
.roas span{font-size:17px;font-weight:600;color:#7a8798;margin-left:6px}
.sub{font-size:13px;color:#5a6b7d;margin-top:9px}
.vs{font-size:12px;color:#8a97a5;margin-top:7px}
h2{font-size:12px;margin:0 0 12px;color:#7a8798;font-weight:700;
   text-transform:uppercase;letter-spacing:.07em}
table{border-collapse:collapse;width:100%;font-size:14px}
th{color:#8a97a5;text-align:right;padding:0 6px 8px;font-weight:600;font-size:11px;
   text-transform:uppercase;letter-spacing:.05em;border-bottom:1px solid #e6ecf3;white-space:nowrap}
th:first-child,td:first-child{text-align:left}
td{padding:9px 6px;border-bottom:1px solid #f0f3f7;text-align:right;white-space:nowrap}
tr:last-child td{border-bottom:none}
.site{font-weight:600}
.tot td{font-weight:700;color:#12355b;border-top:2px solid #dde4ee;border-bottom:none}
.up{color:#0a7d3c;font-weight:600}.dn{color:#c0392b;font-weight:600}.mut{color:#aab4c0}
.big{font-weight:700;font-size:15px}
.scroll{overflow-x:auto}
.now td{background:#f2f7ff}
.gap td{color:#aab4c0;font-style:italic}
.act{padding:10px 0;border-bottom:1px solid #f0f3f7}
.act:last-child{border-bottom:none}
.nm{font-size:13px;font-weight:600}
.dt{font-size:12px;color:#7a8798;margin-top:3px}
.tag{display:inline-block;font-size:10px;font-weight:700;padding:2px 7px;border-radius:3px;
     letter-spacing:.05em;margin-right:7px}
.t-pause{background:#fdeae7;color:#b03024}
.t-review{background:#fff2d4;color:#8a6100}
.t-watch{background:#eef3f9;color:#4a6580}
.ok{font-size:13px;color:#0a7d3c;font-weight:600}
.foot{font-size:11px;color:#94a0ad;text-align:center;line-height:1.7;padding:6px 8px 0}
@media(max-width:640px){.roas{font-size:38px}.wrap{padding:12px 8px 40px}.card{padding:14px}}
"""


def rupee(v):
    return f'&#8377;{v:,.0f}'


def hour_log(day, prows, arows):
    """One row per hour: per-website ROAS, totals, products live."""
    by = {}
    for r in prows + arows:
        by.setdefault(r['slot'], {})[r['portal']] = r
    out = []
    for slot in sorted(by):
        g = by[slot]
        a = g.get('ALL')
        if not a:
            continue
        if not a['has_snap']:
            out.append(f'<tr class="gap"><td>{slot[-5:]}</td>'
                       f'<td colspan="9">no snapshot this hour</td></tr>')
            continue
        if not (a['ad_spend'] or a['shopify_sale']):
            continue
        cells = ''
        for p in PORTALS:
            c = g.get(p)
            cells += (f'<td>{c["roas"]:.2f}</td>' if c and c['ad_spend']
                      else '<td class="mut">&mdash;</td>')
        out.append(
            f'<tr><td>{slot[-5:]}</td>{cells}'
            f'<td>{rupee(a["shopify_sale"])}</td><td>{rupee(a["ad_spend"])}</td>'
            f'<td class="big">{a["roas"]:.2f}</td>'
            f'<td>{rupee(a["active_budget"])}</td>'
            f'<td class="mut">{rupee(a["closed_budget"])}</td>'
            f'<td>{a["products"]}</td></tr>')
    if out:
        out[-1] = out[-1].replace('<tr>', '<tr class="now">', 1)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--snap-db', default='state/camp_snapshots.db')
    ap.add_argument('--ntn-db', default='state/ntn.db')
    ap.add_argument('--out', default='roas-live/index.html')
    ap.add_argument('--days', type=int, default=7, help='days of archive below today')
    args = ap.parse_args()

    now = datetime.now(IST)
    day = now.strftime('%Y-%m-%d')

    prows = build_rows(args.snap_db, args.ntn_db, day)
    tot = summarise(prows) if prows else None
    arows = all_portal_rows(prows) if prows else []

    yday_date = (now - timedelta(days=1)).strftime('%Y-%m-%d')
    yrows = build_rows(args.snap_db, args.ntn_db, yday_date)
    yday = summarise(yrows) if yrows else None

    con = sqlite3.connect(f'file:{args.snap_db}?mode=ro', uri=True)
    lk16, _ = build_both(args.snap_db, exclude_day=day)
    closing = collect(con, day, lk16, lk16, build_first_activity(con))
    con.close()

    a = tot['ALL'] if tot else {'roas': 0, 'rev': 0, 'spend': 0, 'orders': 0, 'products': 0}
    stamp = now.strftime('%d %b %Y, %H:%M IST')

    h = [f'<!doctype html><html lang="en"><head><meta charset="utf-8">',
         '<meta name="viewport" content="width=device-width,initial-scale=1">',
         '<meta http-equiv="refresh" content="600">',      # nudge the tab every 10 min
         # No login gate (operator's call), so at least keep it out of search
         # indexes — this page shows per-website revenue, spend and budgets.
         '<meta name="robots" content="noindex,nofollow,noarchive">',
         f'<title>ROAS {a["roas"]:.2f} — {day}</title>',
         f'<style>{CSS}</style></head><body><div class="wrap">',
         '<div class="bar"><h1>Blended ROAS &mdash; hourly</h1>',
         f'<div class="stamp"><span class="dot"></span>Last updated <b>{stamp}</b></div></div>']

    # hero
    h.append('<div class="card hero">')
    h.append(f'<div class="roas">{a["roas"]:.2f}<span>blended ROAS</span></div>')
    h.append(f'<div class="sub">{rupee(a["rev"])} sales on {rupee(a["spend"])} spend '
             f'&nbsp;&middot;&nbsp; {a["orders"]:,} orders &nbsp;&middot;&nbsp; '
             f'{a["products"]} products live</div>')
    h.append(f'<div class="vs">{rupee(a.get("active_budget", 0))} budget live '
             f'&nbsp;&middot;&nbsp; {rupee(a.get("closed_budget", 0))} closed so far</div>')
    if yday:
        y = yday['ALL']
        d = a['roas'] - y['roas']
        cls = 'up' if d > 0 else 'dn' if d < 0 else 'mut'
        h.append(f'<div class="vs">vs yesterday {y["roas"]:.2f} '
                 f'<span class="{cls}">{d:+.2f}</span></div>')
    h.append(f'<div class="vs">{day}</div></div>')

    # today by website
    if tot:
        h.append('<div class="card"><h2>Today by website</h2><div class="scroll"><table>')
        h.append('<tr><th>Website</th><th>Sales</th><th>Spend</th><th>ROAS</th>'
                 '<th>Yesterday</th><th>Budget live</th><th>Budget closed</th>'
                 '<th>Products</th></tr>')
        for p in PORTALS:
            t = tot[p]
            yv = f'{yday[p]["roas"]:.2f}' if yday else '&mdash;'
            h.append(f'<tr><td class="site">{WEBSITE[p]}</td><td>{rupee(t["rev"])}</td>'
                     f'<td>{rupee(t["spend"])}</td><td class="big">{t["roas"]:.2f}</td>'
                     f'<td class="mut">{yv}</td><td>{rupee(t["active_budget"])}</td>'
                     f'<td class="mut">{rupee(t["closed_budget"])}</td>'
                     f'<td>{t["products"]}</td></tr>')
        h.append(f'<tr class="tot"><td>All</td><td>{rupee(a["rev"])}</td>'
                 f'<td>{rupee(a["spend"])}</td><td>{a["roas"]:.2f}</td><td></td>'
                 f'<td>{rupee(a["active_budget"])}</td>'
                 f'<td>{rupee(a["closed_budget"])}</td>'
                 f'<td>{a["products"]}</td></tr>')
        h.append('</table></div></div>')

    # hourly log — the "saved every hour" section
    rows = hour_log(day, prows, arows)
    h.append('<div class="card"><h2>Hour by hour &mdash; today</h2><div class="scroll"><table>')
    h.append('<tr><th>Hour IST</th>'
             + ''.join(f'<th>{p}</th>' for p in PORTALS)
             + '<th>Sales</th><th>Spend</th><th>ROAS</th>'
               '<th>Budget live</th><th>Budget closed</th><th>Products</th></tr>')
    h.append(''.join(rows) if rows else
             '<tr><td colspan="10" class="mut">no hours recorded yet today</td></tr>')
    h.append('</table></div></div>')

    # previous days
    arch = []
    for i in range(1, args.days + 1):
        d = (now - timedelta(days=i)).strftime('%Y-%m-%d')
        pr = build_rows(args.snap_db, args.ntn_db, d)
        if not pr:
            continue
        t = summarise(pr)['ALL']
        if not t['spend']:
            continue
        arch.append((d, t, summarise(pr)))
    if arch:
        h.append('<div class="card"><h2>Previous days</h2><div class="scroll"><table>')
        h.append('<tr><th>Date</th>' + ''.join(f'<th>{p}</th>' for p in PORTALS)
                 + '<th>Sales</th><th>Spend</th><th>ROAS</th>'
                   '<th>Budget closed</th></tr>')
        for d, t, full in arch:
            cells = ''.join(f'<td>{full[p]["roas"]:.2f}</td>' for p in PORTALS)
            h.append(f'<tr><td>{datetime.strptime(d, "%Y-%m-%d").strftime("%a %d %b")}</td>'
                     f'{cells}<td>{rupee(t["rev"])}</td><td>{rupee(t["spend"])}</td>'
                     f'<td class="big">{t["roas"]:.2f}</td>'
                     f'<td class="mut">{rupee(t["closed_budget"])}</td></tr>')
        h.append('</table></div></div>')

    # decisions
    act = [r for r in closing if r['verdict'] in
           ('PAUSE', 'PAUSE (not whitelisted)', 'REVIEW', 'WATCH')]
    h.append(f'<div class="card"><h2>Needs a decision &mdash; {len(act)}</h2>')
    if not act:
        h.append('<div class="ok">&#10003; Nothing over-spending below target.</div>')
    for r in act[:20]:
        v = r['verdict']
        cls = ('t-pause' if v.startswith('PAUSE') else
               't-review' if v == 'REVIEW' else 't-watch')
        sr = f", {r['success_rate']}% recover" if r['success_rate'] is not None else ''
        mg = f", 3h {r['marginal_3h']:.2f}" if r['marginal_3h'] is not None else ''
        h.append(f'<div class="act"><div class="nm"><span class="tag {cls}">'
                 f'{v.split(" ")[0]}</span>{r["campaign_name"][:70]}</div>'
                 f'<div class="dt">{r["portal"]} &middot; {r["spend_pct"]:.0f}% of budget '
                 f'&middot; ROAS {r["roas"]:.2f}{mg}{sr}</div></div>')
    h.append('</div>')

    n_scale = sum(1 for r in closing if r['verdict'] == 'SCALE')
    h.append(f'<div class="foot">{len(closing)} campaigns tracked &middot; {n_scale} scale '
             f'candidates &middot; recovery rates from {lk16.n_camp_days} past campaign-days '
             f'at target {TARGET_ROAS}<br>'
             'Blended ROAS counts <b>all</b> Shopify revenue including organic and repeat &mdash; '
             'a profitability read per website, not a campaign metric.<br>'
             'Campaign figures are Meta pixel-attributed. Nothing is paused automatically. '
             'Page rebuilds hourly.</div>')
    h.append('</div></body></html>')

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text('\n'.join(h), encoding='utf-8')
    print(f'wrote {out} — ROAS {a["roas"]:.2f}, {len(rows)} hour rows, '
          f'{len(arch)} archived days, {len(act)} decisions, stamp {stamp}')


if __name__ == '__main__':
    main()
