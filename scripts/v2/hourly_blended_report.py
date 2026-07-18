#!/usr/bin/env python3
"""
hourly_blended_report.py — REPORT 1: hourly blended ROAS per portal.

Columns, as specified by the operator:
    A portal | B Shopify sale | C ad spend | D no. of product suggestions

"Product suggestions" = how many DISTINCT products are live on ads in that hour.
If it reads 20 at 14:00 and 15 at 15:00, five products were switched off during
that hour — that drop is the signal to watch.

ROAS here is BLENDED: real Shopify revenue ÷ real Meta spend, at portal grain.
It is not comparable to the per-campaign pixel ROAS on the live-roas dashboard;
blended is lower and truer. Only ~24% of Shopify orders carry a utm_campaign,
which is exactly why this report refuses to pretend to campaign-level accuracy.

Sources: state/camp_snapshots.db (Meta, hourly) + state/ntn.db (Shopify orders).

Usage:
  python3 scripts/v2/hourly_blended_report.py \
      --snap-db state/camp_snapshots.db --ntn-db state/ntn.db \
      --sheet 1eW2_qPdsKJ8zAV5-hsXA5HtfVH9NwDhQLyHYGKz5hXk --whatsapp
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from portal_hourly import (  # noqa: E402
    PORTALS, all_portal_rows, build_rows, summarise, today_ist,
)

ROW_ORDER = {'SM': 0, 'SML': 1, 'NBP': 2, 'ALL': 3}

IST = timezone(timedelta(hours=5, minutes=30))

HEADER = ['Hour (IST)', 'Portal', 'Shopify Sale ₹', 'Ad Spend ₹',
          'Product Suggestions', 'Blended ROAS', 'Orders', 'Δ Products',
          'Live Campaigns', 'Cum. Spend ₹']


def with_deltas(rows: list[dict]) -> list[dict]:
    """Annotate each row with the change in live-product count vs the same
    portal's previous hour — the '5 got closed this hour' number."""
    prev: dict[str, int | None] = {}
    for r in sorted(rows, key=lambda x: (x['slot'], ROW_ORDER.get(x['portal'], 9))):
        p = r['portal']
        if not r['has_snap']:
            # Unknown hour — carry `prev` forward untouched so the next real
            # observation is compared against the last real one.
            r['dprod'] = ''
            continue
        r['dprod'] = '' if prev.get(p) is None else r['products'] - prev[p]
        prev[p] = r['products']
    return rows


def to_values(rows: list[dict]) -> list[list]:
    out = []
    for r in sorted(rows, key=lambda x: (x['slot'], ROW_ORDER.get(x['portal'], 9))):
        # Skip fully empty (portal, hour) cells — no spend, no sales, no products.
        if not (r['ad_spend'] or r['shopify_sale'] or r['products']):
            continue
        d = r['dprod']
        if r['has_snap']:
            spend, products, roas, camps = (round(r['ad_spend']), r['products'],
                                            r['roas'], r['campaigns'])
            dp = f'+{d}' if isinstance(d, int) and d > 0 else (str(d) if d != '' else '')
        else:
            # No Meta snapshot this hour — Shopify sales are real, everything
            # Meta-derived is unknown. Blank beats a misleading zero.
            spend = products = roas = camps = 'no snapshot'
            dp = ''
        out.append([
            r['hour'], r['portal'], round(r['shopify_sale']), spend,
            products, roas, r['orders'], dp, camps,
            round(r['cum_spend']) if r['has_snap'] else '',
        ])
    return out


def digest(tot: dict, day: str, rows: list[dict]) -> str:
    """Short WhatsApp body — latest hour plus the day so far."""
    latest = max((r['slot'] for r in rows if r['has_snap'] and r['ad_spend']), default=None)
    lines = [f"HOURLY BLENDED ROAS — {day}"]
    if latest:
        lines.append(f"Latest hour {latest[-5:]}:")
        for p in PORTALS:
            r = next((x for x in rows if x['slot'] == latest and x['portal'] == p), None)
            if r and (r['ad_spend'] or r['shopify_sale']):
                lines.append(f"  {p}: sale ₹{r['shopify_sale']:,.0f} | spend ₹{r['ad_spend']:,.0f} "
                             f"| ROAS {r['roas']:.2f} | {r['products']} products")
    lines.append("Day so far:")
    for p in PORTALS:
        t = tot[p]
        if t['spend'] or t['rev']:
            lines.append(f"  {p}: ₹{t['rev']:,.0f} / ₹{t['spend']:,.0f} = {t['roas']:.2f} "
                         f"({t['products']} products live)")
    a = tot['ALL']
    lines.append(f"TOTAL: ₹{a['rev']:,.0f} / ₹{a['spend']:,.0f} = {a['roas']:.2f}")
    return '\n'.join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--snap-db', default='state/camp_snapshots.db')
    ap.add_argument('--ntn-db', default='state/ntn.db')
    ap.add_argument('--day', default=None, help='YYYY-MM-DD IST (default today)')
    ap.add_argument('--sheet', default=os.environ.get('LIVE_ROAS_SHEET_ID',
                                                      '1eW2_qPdsKJ8zAV5-hsXA5HtfVH9NwDhQLyHYGKz5hXk'))
    ap.add_argument('--tab', default='Hourly Blended ROAS')
    ap.add_argument('--sa', default=os.environ.get('GOOGLE_SERVICE_ACCOUNT_FILE',
                                                   'google-service-account.json'))
    ap.add_argument('--whatsapp', action='store_true', help='also send the digest to WhatsApp')
    ap.add_argument('--no-sheet', action='store_true', help='print only, skip the sheet write')
    ap.add_argument('--xlsx', default=None, help='also write a styled .xlsx to this path')
    args = ap.parse_args()

    day = args.day or today_ist()
    for p in (args.snap_db, args.ntn_db):
        if not Path(p).exists():
            print(f"FATAL: missing DB {p}")
            sys.exit(1)

    portal_rows = build_rows(args.snap_db, args.ntn_db, day)
    tot = summarise(portal_rows)              # portal rows only — ALL would double-count
    rows = with_deltas(portal_rows + all_portal_rows(portal_rows))
    values = to_values(rows)

    if not values:
        print(f"no hourly data for {day} — nothing to write")
        return

    print(f"{day}: {len(values)} portal-hour rows")
    for p in PORTALS:
        t = tot[p]
        print(f"  {p:4} sale ₹{t['rev']:>10,.0f}  spend ₹{t['spend']:>10,.0f}  "
              f"ROAS {t['roas']:>5.2f}  products {t['products']}")
    a = tot['ALL']
    print(f"  ALL  sale ₹{a['rev']:>10,.0f}  spend ₹{a['spend']:>10,.0f}  ROAS {a['roas']:>5.2f}")

    if not args.no_sheet:
        import gspread
        updated = datetime.now(IST).strftime('%d %b %Y, %H:%M IST')
        top = [[f"⏱️ HOURLY BLENDED ROAS — {day} · updated {updated} · "
                f"Shopify sales ÷ Meta spend per portal per hour · "
                f"'Product Suggestions' = distinct products live on ads that hour · "
                f"day ROAS {a['roas']:.2f} (₹{a['rev']:,.0f} / ₹{a['spend']:,.0f})"]]
        sheet_values = top + [HEADER] + values
        gc = gspread.service_account(filename=args.sa)
        sh = gc.open_by_key(args.sheet)
        try:
            w = sh.worksheet(args.tab)
            w.clear()
        except gspread.WorksheetNotFound:
            w = sh.add_worksheet(title=args.tab, rows=max(len(sheet_values) + 40, 200),
                                 cols=len(HEADER) + 2)
        w.update(range_name='A1', values=sheet_values)
        try:
            w.freeze(rows=2)
            w.format('A1:J2', {'textFormat': {'bold': True}})
        except Exception:
            pass
        print(f"wrote '{args.tab}' ({len(values)} rows)")

    if args.xlsx:
        from xlsx_out import new_workbook, write_table
        wb = new_workbook()
        updated = datetime.now(IST).strftime('%d %b %Y, %H:%M IST')

        ws = wb.create_sheet('Hourly Blended ROAS')
        summary_hdr = ['Portal', 'Shopify Sale ₹', 'Ad Spend ₹', 'Blended ROAS',
                       'Orders', 'Products Live']
        summary = [[p, round(tot[p]['rev']), round(tot[p]['spend']), tot[p]['roas'],
                    tot[p]['orders'], tot[p]['products']] for p in PORTALS]
        summary.append(['ALL', round(a['rev']), round(a['spend']), a['roas'],
                        a['orders'], a['products']])
        nxt = write_table(
            ws, f'⏱️ HOURLY BLENDED ROAS — {day} · updated {updated} · '
                f'Shopify sales ÷ Meta spend per portal per hour',
            summary_hdr, summary, band='DAY TOTALS',
            numfmt={2: '#,##0', 3: '#,##0', 4: '0.00'}, center_cols={1, 4, 5, 6})

        write_table(
            ws, '', HEADER, values, start=nxt,
            band="BY HOUR — 'Product Suggestions' = distinct products live on ads that hour; "
                 "Δ Products is the change vs the previous hour",
            numfmt={3: '#,##0', 4: '#,##0', 6: '0.00', 10: '#,##0'},
            center_cols={1, 2, 5, 6, 7, 8, 9})

        Path(args.xlsx).parent.mkdir(parents=True, exist_ok=True)
        wb.save(args.xlsx)
        print(f'wrote {args.xlsx}')

    if args.whatsapp:
        from wa_notify import send
        sent, failed = send(digest(tot, day, rows), header='⏱️ Hourly Blended ROAS')
        print(f"whatsapp: {sent} sent, {failed} failed")


if __name__ == '__main__':
    main()
