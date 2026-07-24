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
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from portal_hourly import (  # noqa: E402
    PORTALS, all_portal_rows, build_rows, closures, latest_snapshot_ts,
    slot_times, summarise,
)
from success_lookup import TARGET_ROAS, build_both  # noqa: E402
from camp_closing import build_first_activity, collect  # noqa: E402
from daily_finals import load as load_finals, finals_for  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
WEBSITE = {'SM': 'Studd Muffyn', 'SML': 'SM Life', 'NBP': 'Nuskhe by Paras'}

# One colour per website, used for the dot, the row accent and the hour-summary
# chips. Chosen to stay distinguishable for the common forms of colour blindness
# (blue / teal / amber differ in lightness as well as hue, so they do not rely on
# red-green discrimination).
PORTAL_COLOR = {'SM': '#4f46e5', 'SML': '#0d9488', 'NBP': '#d97706'}

# Minimum gap between Meta/Shopify pulls, matching MIN_GAP in
# .github/workflows/roas-email.yml. The workflow still runs every ~10 minutes
# but only calls the APIs once this much time has passed, so the data refreshes
# roughly hourly. KEEP THE TWO IN SYNC.
MIN_PULL_GAP_MIN = 55

# Liveness thresholds follow from that gap: anything under ~70 minutes means a
# pull is landing on schedule. Tighter values would read DELAYED permanently.
LIVE_MAX_MIN = 70
DELAYED_MAX_MIN = 95


# Pulls happen at the top of each hour (:00) so every hour is captured
# complete — see the "Pull Meta at the top of the hour" step in
# .github/workflows/roas-email.yml.
PULL_MINUTE = 0


def next_update(now, last_pull=None):
    """The next top-of-hour pull. Approximate (GitHub skips ticks) so shown "~"."""
    return now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)

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
.nxt{display:block;color:#94a0ad;font-size:11px;margin-top:3px}
@media(min-width:641px){.stamp{text-align:right}}
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
.pdot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:8px;
      vertical-align:1px}
tr.prow td:first-child{border-left:3px solid transparent;padding-left:10px}
tr.p-SM  td:first-child{border-left-color:#4f46e5}
tr.p-SML td:first-child{border-left-color:#0d9488}
tr.p-NBP td:first-child{border-left-color:#d97706}
.chip{display:inline-block;font-size:11px;font-weight:600;padding:2px 8px;
      border-radius:10px;margin-right:6px;color:#fff}
.c-SM{background:#4f46e5}.c-SML{background:#0d9488}.c-NBP{background:#d97706}
.hsum{display:flex;flex-wrap:wrap;gap:6px;align-items:center}
.hlabel{font-weight:700;color:#12355b;min-width:64px}
.asof{font-size:11px;color:#94a0ad;margin-left:2px}
.tot td{font-weight:700;color:#12355b;border-top:2px solid #dde4ee;border-bottom:none}
.up{color:#0a7d3c;font-weight:600}.dn{color:#c0392b;font-weight:600}.mut{color:#aab4c0}
.big{font-weight:700;font-size:15px}
.scroll{overflow-x:auto}
.now td{background:#f2f7ff}
.gap td{color:#aab4c0;font-style:italic}
.sub2{display:block;font-size:10px;color:#9aa6b2;font-weight:400;margin-top:2px}
details{margin-top:10px;border-top:1px solid #f0f3f7;padding-top:10px}
details:first-of-type{border-top:none}
summary{cursor:pointer;font-size:13px;font-weight:600;color:#12355b;padding:6px 0;
        list-style:none;display:flex;justify-content:space-between;gap:12px}
summary::-webkit-details-marker{display:none}
summary::after{content:'\25be';color:#aab4c0;font-size:11px}
details[open] summary::after{content:'\25b4'}
summary .m{font-weight:400;color:#7a8798;font-size:12px}
.run{color:#0a7d3c;font-weight:700}
.badge{display:inline-block;font-size:11px;font-weight:800;letter-spacing:.06em;
       padding:3px 9px;border-radius:11px;margin-right:8px;vertical-align:1px}
.badge.live{background:#e3f5e9;color:#0a7d3c}
.badge.warnb{background:#fff2d4;color:#8a6100}
.badge.dead{background:#fdeae7;color:#b03024}
#age{font-size:12px;color:#5a6b7d}
.dot.stale{background:#e08a00}
.warn{color:#b06800;font-weight:600;font-size:11px}
.pulse{animation:pl 1.1s ease-in-out infinite}
@keyframes pl{0%,100%{opacity:1}50%{opacity:.35}}
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
.dlcard{display:flex;flex-wrap:wrap;align-items:center;gap:16px;justify-content:space-between}
.dlleft{min-width:0}
.dlmeta{font-size:12px;color:#5a6b7d;line-height:1.7;margin-top:4px}
.dlmeta b{color:#12355b}
.dlmeta .k{display:inline-block;min-width:70px;color:#94a0ad}
.dlbtn{display:inline-flex;align-items:center;gap:9px;background:#12355b;color:#fff;
       text-decoration:none;font-size:13px;font-weight:700;padding:12px 20px;border-radius:8px;
       box-shadow:0 1px 2px rgba(16,32,56,.18);white-space:nowrap}
.dlbtn:hover{background:#0d2947}
.dlbtn .ic{font-size:16px;line-height:1}
.dlsub{font-size:11px;color:#94a0ad;margin-top:6px}
@media(max-width:640px){.roas{font-size:38px}.wrap{padding:12px 8px 40px}.card{padding:14px}
  .dlcard{gap:12px}.dlbtn{width:100%;justify-content:center}}
"""


def rupee(v):
    return f'&#8377;{v:,.0f}'


# Each saved hour repeats the same columns as "Today by website", so the two
# read identically — the hourly block is that table frozen at that hour.
HOUR_COLS = ('<tr><th>Website</th><th>Sales</th><th>Orders</th><th>Spend</th>'
             '<th>ROAS</th><th>Budget live</th><th>Budget closed</th>'
             '<th>Products</th></tr>')


def hour_blocks(prows, arows, open_last=False, times=None, now=None):
    """One collapsible block per hour holding the full per-website breakdown,
    cumulative as at that hour.

    Values are DAY-TO-DATE, not that hour in isolation: a single hour swings on
    a handful of orders (00:00 read 4.49 on three orders while the day tracked
    1.21), so the saved snapshot answers "where did the day stand at 07:00",
    which is the question worth asking of an archive.
    """
    by = {}
    for r in prows + arows:
        by.setdefault(r['slot'], {})[r['portal']] = r

    out, slots = [], sorted(by)
    for i, slot in enumerate(slots):
        g = by[slot]
        a = g.get('ALL')
        if not a or not a['has_snap'] or not (a['cum_spend'] or a['cum_sales']):
            continue
        chips = ''.join(
            f'<span class="chip c-{p}">{p} {g[p]["cum_roas"]:.2f}</span>'
            for p in PORTALS if g.get(p) and g[p]['cum_spend'])
        body = ''
        for p in PORTALS:
            c = g.get(p)
            if not c or not (c['cum_spend'] or c['active_budget']):
                continue
            body += (
                f'<tr class="prow p-{p}"><td class="site">'
                f'<span class="pdot" style="background:{PORTAL_COLOR[p]}"></span>'
                f'{WEBSITE[p]}</td>'
                f'<td>{rupee(c["cum_sales"])}</td><td>{c["cum_orders"]:,}</td>'
                f'<td>{rupee(c["cum_spend"])}</td>'
                f'<td class="big">{c["cum_roas"]:.2f}</td>'
                f'<td>{rupee(c["active_budget"])}</td>'
                f'<td class="mut">{rupee(c["closed_budget"])}</td>'
                f'<td>{c["products"]}</td></tr>')
        body += (
            f'<tr class="tot"><td>All</td><td>{rupee(a["cum_sales"])}</td>'
            f'<td>{a["cum_orders"]:,}</td><td>{rupee(a["cum_spend"])}</td>'
            f'<td>{a["cum_roas"]:.2f}</td><td>{rupee(a["active_budget"])}</td>'
            f'<td>{rupee(a["closed_budget"])}</td><td>{a["products"]}</td></tr>')

        is_last = (i == len(slots) - 1)
        # An hour labelled 12:00 but measured at 12:08 covers eight minutes, not
        # sixty. Say so, otherwise it reads as "the numbers stopped moving".
        ts = (times or {}).get(slot)
        meas = ''
        if ts:
            hhmm = ts[11:16]
            partial = is_last and now is not None and now.strftime('%Y-%m-%d %H:00') == slot
            meas = (f'<span class="asof">as of {hhmm}'
                    + (' &middot; hour still running' if partial else '')
                    + '</span>')
        out.append(
            f'<details{" open" if (open_last and is_last) else ""}>'
            f'<summary><span class="hsum"><span class="hlabel">{slot[-5:]}</span>'
            f'{chips}{meas}</span>'
            f'<span class="m">ROAS {a["cum_roas"]:.2f} &middot; {rupee(a["cum_sales"])} on '
            f'{rupee(a["cum_spend"])} &middot; {a["products"]} products</span></summary>'
            f'<div class="scroll"><table>{HOUR_COLS}{body}</table></div></details>')
    return out


CLOSE_HEAD = ('<tr><th>Closed at</th><th>Website</th><th>Campaign</th>'
              '<th>Spend</th><th>% of budget</th><th>ROAS</th></tr>')


def closure_rows(items):
    """Newest closure first. '~' because we know the campaign was live at the
    previous snapshot and paused at this one — the actual moment is inside that
    ~10 minute window, not the timestamp itself."""
    out = []
    for r in items:
        when = (f'<span class="mut">before {r["closed_ts"][11:16]}</span>'
                if r['before'] else f'~{r["closed_ts"][11:16]}')
        out.append(
            f'<tr><td>{when}</td><td class="site">{r["portal"]}</td>'
            f'<td style="text-align:left">{r["campaign_name"][:64]}</td>'
            f'<td>{rupee(r["spend"])}</td><td>{r["spend_pct"]:.0f}%</td>'
            f'<td class="big">{r["roas"]:.2f}</td></tr>')
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--snap-db', default='state/camp_snapshots.db')
    ap.add_argument('--ntn-db', default='state/ntn.db')
    ap.add_argument('--out', default='roas-live/index.html')
    ap.add_argument('--days', type=int, default=7, help='days of archive below today')
    ap.add_argument('--finals', default='state/daily_finals.json',
                    help='frozen end-of-day totals for completed days')
    args = ap.parse_args()

    now = datetime.now(IST)
    day = now.strftime('%Y-%m-%d')

    prows = build_rows(args.snap_db, args.ntn_db, day)
    tot = summarise(prows) if prows else None
    arows = all_portal_rows(prows) if prows else []

    # Yesterday and older days use FROZEN finals (all-day Shopify sales / Meta's
    # final daily spend), not the snapshot sum — the last snapshot of a day is
    # short, badly so now that pulls land at the top of the hour. Falls back to
    # the snapshot computation only if a day hasn't been frozen yet.
    finals = load_finals(args.finals)
    yday_date = (now - timedelta(days=1)).strftime('%Y-%m-%d')
    yday = finals_for(finals, yday_date)
    if yday is None:
        yrows = build_rows(args.snap_db, args.ntn_db, yday_date)
        yday = summarise(yrows) if yrows else None

    con = sqlite3.connect(f'file:{args.snap_db}?mode=ro', uri=True)
    lk16, _ = build_both(args.snap_db, exclude_day=day)
    closing = collect(con, day, lk16, lk16, build_first_activity(con))
    con.close()

    a = tot['ALL'] if tot else {'roas': 0, 'rev': 0, 'spend': 0, 'orders': 0, 'products': 0}
    stamp = now.strftime('%d %b %Y, %H:%M IST')
    # The number that matters is when META SPEND was last measured, not when
    # this HTML was generated. Reporting build time as "last updated" made a
    # 72-minute-old spend figure look current.
    snap_ts = latest_snapshot_ts(args.snap_db, day)
    if snap_ts:
        sdt = datetime.fromisoformat(snap_ts)
        data_age = int((now - sdt).total_seconds() // 60)
        data_txt = f'{sdt:%d %b, %H:%M} IST'
    else:
        data_age, data_txt = 0, 'no snapshot yet'
    nxt = next_update(now, sdt if snap_ts else None)
    mins = round((nxt - now).total_seconds() / 60)
    # Once a pull is overdue, printing its scheduled time reads as a time in the
    # past. Say how late it is instead — that is the useful signal.
    nxt_txt = (f'{nxt:%H:%M} IST &middot; in {mins} min' if mins > 0
               else f'due now &middot; {abs(mins)} min overdue')

    h = [f'<!doctype html><html lang="en"><head><meta charset="utf-8">',
         '<meta name="viewport" content="width=device-width,initial-scale=1">',
         # No login gate (operator's call), so at least keep it out of search
         # indexes — this page shows per-website revenue, spend and budgets.
         '<meta name="robots" content="noindex,nofollow,noarchive">',
         f'<title>ROAS {a["roas"]:.2f} — {day}</title>',
         f'<style>{CSS}</style></head><body><div class="wrap">',
         '<div class="bar"><h1>Blended ROAS &mdash; hourly</h1>',
         f'<div class="stamp">'
         f'<span id="badge" class="badge live">&#9679; LIVE</span>'
         f'<span id="age"><b>Today&rsquo;s figures through {data_txt}</b> '
         f'&middot; {data_age} min old</span>'
         f'<span class="nxt">Meta &amp; Shopify are measured once at the top of each '
         f'hour, so this is a complete hour &mdash; live ~2 min after it turns. '
         f'Yesterday and earlier are frozen full-day totals, untouched by the hourly pull.</span>'
         f'<span class="nxt" id="chk">checking\u2026</span>'
         f'<span class="nxt" id="nxt">Next update ~{nxt_txt}</span></div></div>']

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

    # Downloadable products report (Excel). The sidecar json is written next to
    # the xlsx by build_products_report.py; if the report build was skipped or
    # failed this run, we simply omit the card rather than link a missing file.
    rjson = Path(args.out).parent / 'reports' / 'products-report.json'
    if rjson.exists():
        try:
            rmeta = json.loads(rjson.read_text())
            g = rmeta.get('grand', {})
            h.append('<div class="card"><h2>Reports</h2><div class="dlcard">')
            h.append('<div class="dlleft"><div style="font-weight:700;color:#12355b;font-size:14px">'
                     'All Portals &middot; Products Report</div>'
                     f'<div class="dlmeta">'
                     f'<span class="k">Generated</span> <b>{rmeta.get("stamp","")}</b><br>'
                     f'<span class="k">Today</span> ROAS <b>{g.get("today_roas",0):.2f}</b> '
                     f'&middot; {rupee(g.get("today_spend",0))} spend &rarr; {rupee(g.get("today_rev",0))} sales<br>'
                     f'<span class="k">10-day</span> ROAS <b>{g.get("d10_roas",0):.2f}</b> '
                     f'&middot; {rupee(g.get("d10_spend",0))} &rarr; {rupee(g.get("d10_rev",0))}</div>'
                     '<div class="dlsub">Per-product spend, budget &amp; ROAS across SM / NBP / SML '
                     '&middot; Meta 1d-view + 7d-click &middot; refreshes hourly</div></div>')
            h.append('<a class="dlbtn" href="reports/products-report.xlsx" download>'
                     '<span class="ic">&#8681;</span> Download Excel</a>')
            h.append('</div></div>')
        except Exception:
            pass

    # today by website
    if tot:
        h.append('<div class="card"><h2>Today by website</h2><div class="scroll"><table>')
        h.append('<tr><th>Website</th><th>Sales</th><th>Orders</th><th>Spend</th>'
                 '<th>ROAS</th><th>Yesterday</th><th>Budget live</th>'
                 '<th>Budget closed</th><th>Products</th></tr>')
        for p in PORTALS:
            t = tot[p]
            yv = f'{yday[p]["roas"]:.2f}' if yday else '&mdash;'
            h.append(f'<tr class="prow p-{p}"><td class="site">'
                     f'<span class="pdot" style="background:{PORTAL_COLOR[p]}"></span>'
                     f'{WEBSITE[p]}</td><td>{rupee(t["rev"])}</td>'
                     f'<td>{t["orders"]:,}</td>'
                     f'<td>{rupee(t["spend"])}</td><td class="big">{t["roas"]:.2f}</td>'
                     f'<td class="mut">{yv}</td><td>{rupee(t["active_budget"])}</td>'
                     f'<td class="mut">{rupee(t["closed_budget"])}</td>'
                     f'<td>{t["products"]}</td></tr>')
        h.append(f'<tr class="tot"><td>All</td><td>{rupee(a["rev"])}</td>'
                 f'<td>{a["orders"]:,}</td>'
                 f'<td>{rupee(a["spend"])}</td><td>{a["roas"]:.2f}</td><td></td>'
                 f'<td>{rupee(a["active_budget"])}</td>'
                 f'<td>{rupee(a["closed_budget"])}</td>'
                 f'<td>{a["products"]}</td></tr>')
        h.append('</table></div></div>')

    # hourly log — the "saved every hour" section
    stimes = slot_times(args.snap_db, day)
    rows = hour_blocks(prows, arows, open_last=True, times=stimes, now=now)
    h.append(f'<div class="card"><h2>Saved every hour &mdash; today ({len(rows)})</h2>')
    h.append(''.join(rows) if rows else
             '<div class="mut">no hours recorded yet today</div>')
    h.append('<div class="foot" style="text-align:left;padding-left:0">'
             'Every hour is saved with the same columns as Today by website, '
             'cumulative to that point in the day. \u201cAs of\u201d is when the '
             'numbers were actually pulled \u2014 the newest hour is normally only '
             'part-way through, so it will look close to the one before it.</div>')
    h.append('</div>')

    # previous days
    # Previous days keep their FULL hour-by-hour table, collapsed by date.
    # Rebuilt from the databases every time, so a day the page never rendered
    # live still appears here complete.
    arch = []
    for i in range(1, args.days + 1):
        d = (now - timedelta(days=i)).strftime('%Y-%m-%d')
        pr = build_rows(args.snap_db, args.ntn_db, d)
        if not pr:
            continue
        # Day totals: sales/spend/ROAS/orders from the frozen finals (accurate),
        # budgets kept from the snapshot (finals don't carry a budget state).
        full = summarise(pr)
        fin = finals_for(finals, d)
        if fin:
            for k in list(PORTALS) + ['ALL']:
                full[k].update({m: fin[k][m] for m in ('rev', 'spend', 'roas', 'orders')})
        if not full['ALL']['spend']:
            continue
        arch.append((d, full, pr, all_portal_rows(pr)))

    if arch:
        h.append('<div class="card"><h2>Saved hours &mdash; previous days</h2>')
        for d, full, pr, ar in arch:
            t = full['ALL']
            label = datetime.strptime(d, '%Y-%m-%d').strftime('%a %d %b %Y')
            per = ' &middot; '.join(f'{p} {full[p]["roas"]:.2f}' for p in PORTALS)
            h.append(
                f'<details><summary><span>{label}</span>'
                f'<span class="m">ROAS {t["roas"]:.2f} &middot; {rupee(t["rev"])} on '
                f'{rupee(t["spend"])} &middot; {t["orders"]:,} orders &middot; {per} '
                f'&middot; {rupee(t["closed_budget"])} closed</span></summary>'
                + ''.join(hour_blocks(pr, ar, times=slot_times(args.snap_db, d)))
                + (lambda cl: (f'<h2 style="margin-top:16px">Closed that day '
                               f'({len(cl)})</h2><div class="scroll"><table>'
                               + CLOSE_HEAD + ''.join(closure_rows(cl))
                               + '</table></div>') if cl else '')(
                      closures(args.snap_db, d))
                + '</details>')
        h.append('</div>')

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

    # Closed campaigns, newest first — reconstructed from the status history so
    # a closure during an hour the page never rendered still appears.
    closed_today = closures(args.snap_db, day)
    h.append(f'<div class="card"><h2>Closed campaigns &mdash; today '
             f'({len(closed_today)})</h2>')
    if closed_today:
        h.append('<div class="scroll"><table>' + CLOSE_HEAD
                 + ''.join(closure_rows(closed_today)) + '</table></div>')
        h.append('<div class="foot" style="text-align:left;padding-left:0">'
                 'Time is the first snapshot that saw the campaign paused, so the '
                 'close happened within about 10 minutes before it. '
                 '&ldquo;before HH:MM&rdquo; means it was already off when we '
                 'first saw it that day.</div>')
    else:
        h.append('<div class="ok">&#10003; Nothing closed yet today.</div>')
    h.append('</div>')

    n_scale = sum(1 for r in closing if r['verdict'] == 'SCALE')
    h.append(f'<div class="foot">{len(closing)} campaigns tracked &middot; {n_scale} scale '
             f'candidates &middot; recovery rates from {lk16.n_camp_days} past campaign-days '
             f'at target {TARGET_ROAS}<br>'
             'Blended ROAS counts <b>all</b> Shopify revenue including organic and repeat &mdash; '
             'a profitability read per website, not a campaign metric.<br>'
             'Campaign figures are Meta pixel-attributed. Nothing is paused automatically. '
             'Meta and Shopify are pulled at the top of each hour, so each hour is '
             'captured complete and the board is live within a couple of minutes.</div>')
    # Live status: counts down to the next scheduled rebuild, flips to a pulsing
    # "Refreshing now" once due, then reloads to pick up the new deploy. Paced at
    # 45s so a late build (GitHub queueing) doesn't hammer the page.
    # Liveness monitor. Polls status.json every 2 minutes (no-store, so never a
    # cached answer) and reports whether the pipeline is actually alive rather
    # than just when this HTML happened to be generated. If the poll comes back
    # with a newer data_ts, the page reloads itself so what you are looking at
    # is never behind what has been published.
    #
    # LIVE_MAX_MIN is 15 because that is the real floor: builds land roughly
    # every 5-10 minutes (cron plus the keepalive heartbeat), so anything fresher
    # than 15 minutes means the pipeline is keeping up. Past 25 it is degraded,
    # past 45 something is broken.
    h.append(f'''<script>
var DATA_TS = "{snap_ts or ''}", POLL = 120000,
    LIVE_MAX = {LIVE_MAX_MIN}, DELAYED_MAX = {DELAYED_MAX_MIN};
function fmt(t){{ return t.toTimeString().slice(0,8); }}
function paint(ageMin, checkedAt, ok){{
  var b=document.getElementById('badge'), a=document.getElementById('age'),
      c=document.getElementById('chk');
  if(!b) return;
  if(!ok){{ b.className='badge dead'; b.innerHTML='&#9679; CHECK FAILED'; }}
  else if(ageMin>DELAYED_MAX){{ b.className='badge dead'; b.innerHTML='&#9679; NOT LIVE'; }}
  else if(ageMin>LIVE_MAX){{ b.className='badge warnb'; b.innerHTML='&#9679; DELAYED'; }}
  else {{ b.className='badge live'; b.innerHTML='&#9679; LIVE'; }}
  if(a) a.textContent='data '+ageMin+' min old';
  if(c) c.textContent='live status checked '+fmt(checkedAt)+' \u00b7 rechecks every 2 min';
}}
function check(){{
  fetch('status.json?t='+Date.now(), {{cache:'no-store'}})
    .then(function(r){{ return r.json(); }})
    .then(function(j){{
      var age = Math.max(0, Math.round((Date.now()-new Date(j.data_ts).getTime())/60000));
      paint(age, new Date(), true);
      if(j.data_ts && DATA_TS && j.data_ts !== DATA_TS) location.reload();
    }})
    .catch(function(){{ paint(999, new Date(), false); }});
}}
check(); setInterval(check, POLL);

var NEXT={int(nxt.timestamp() * 1000)};
function tick(){{
  var el=document.getElementById('nxt'); if(!el) return;
  var d=NEXT-Date.now();
  if(d>0){{
    var m=Math.floor(d/60000), s=Math.floor(d%60000/1000);
    el.innerHTML='Next pull ~'+(m>0?m+'m ':'')+('0'+s).slice(-2)+'s';
  }} else {{
    var late=Math.floor((Date.now()-NEXT)/60000);
    el.innerHTML='<span class="run pulse">&#9679; Pull due'
                 +(late>2?' \u00b7 '+late+'m overdue':'\u2026')+'</span>';
  }}
}}
tick(); setInterval(tick,1000);
</script>''')
    h.append('</div></body></html>')

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text('\n'.join(h), encoding='utf-8')

    # Heartbeat the page polls every 2 minutes. Kept tiny and served no-store
    # (see roas-live/vercel.json) so the check is cheap and never cached — the
    # page can tell whether the pipeline is alive without reloading itself.
    (out.parent / 'status.json').write_text(json.dumps({
        'data_ts': snap_ts,
        'data_age_min': data_age,
        'built_ts': now.isoformat(timespec='seconds'),
        'next_update_ts': nxt.isoformat(timespec='seconds'),
        'roas': a['roas'], 'sales': a['rev'], 'spend': a['spend'],
        'orders': a['orders'], 'products': a['products'],
    }, indent=1), encoding='utf-8')
    print(f'wrote {out} — ROAS {a["roas"]:.2f}, {len(rows)} hour rows, '
          f'{len(arch)} archived days, {len(act)} decisions, stamp {stamp}, '
          f'next ~{nxt:%H:%M} IST')


if __name__ == '__main__':
    main()
