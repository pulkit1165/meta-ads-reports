#!/usr/bin/env python3
"""
Category-Wise SPEND backfill → Google Sheet (historical date tabs).

The daily live job (category_budget_sheet.py) writes BUDGET + spend going
forward, but for PAST dates the live budget snapshot was never recorded per
portal/funnel (campaign_budget_history is empty). What IS stored per date is
actual spend (meta_ads_daily). So this one-time backfill rebuilds older date
tabs in the SAME portal-wise + Sales/Retarget layout, using each date's actual
full-day spend (from the local SQLite DB).

Layout per tab (mirrors the live job, spend instead of budget):
  • ALL PORTALS — Category spend rollup
  • ALL PORTALS — Sales vs Retarget spend
  • Per-portal (SM, SML, NBP) — the same two tables

Usage:
  python3 scripts/v2/category_spend_backfill.py 2026-06-04 2026-06-05 ...
  python3 scripts/v2/category_spend_backfill.py            # = all OLD-format tabs
"""
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _utils import IST, db_connect  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from product_catalogue import derive_category_v2  # noqa: E402

# Reuse the live job's sheet helpers + colours + funnel rule so layout matches.
from category_budget_sheet import (  # noqa: E402
    SHEET_ID, PORTAL_ORDER, funnel_type, open_sheet,
    C_TITLE, C_SECTION, C_COLHDR, C_TOTAL, WHITE,
)


def collect(conn, date):
    """List of campaign dicts {portal, category, funnel, spend} for `date`."""
    rows = conn.execute(
        '''SELECT portal, campaign_id,
                  MAX(campaign_name)  AS name,
                  COALESCE(SUM(spend), 0) AS spend
           FROM meta_ads_daily
           WHERE date = ? AND campaign_id IS NOT NULL
           GROUP BY campaign_id''',
        (date,),
    ).fetchall()
    camps = []
    for portal, cid, name, spend in rows:
        if (spend or 0) <= 0:
            continue
        camps.append({
            "portal": portal or "?",
            "category": derive_category_v2(name or ""),
            "funnel": funnel_type(name or ""),
            "spend": float(spend or 0),
        })
    return camps


def cat_rollup(camps):
    agg = defaultdict(lambda: [0, 0.0])
    for c in camps:
        agg[c["category"]][0] += 1
        agg[c["category"]][1] += c["spend"]
    return sorted(((k, v[0], v[1]) for k, v in agg.items()), key=lambda r: -r[2])


def funnel_rollup(camps):
    agg = {t: [0, 0.0] for t in ("Sales", "Retarget", "Other")}
    for c in camps:
        agg[c["funnel"]][0] += 1
        agg[c["funnel"]][1] += c["spend"]
    return [(t, agg[t][0], agg[t][1]) for t in ("Sales", "Retarget", "Other")]


def write_tab(sh, date, camps):
    titles = {w.title: w for w in sh.worksheets()}
    ws = titles.get(date) or sh.add_worksheet(title=date, rows=200, cols=4)

    values, section_rows, colhdr_rows, total_rows = [], [], [], []
    money_ranges, pct_ranges = [], []

    def emit(*cells):
        values.append(list(cells))

    def add_table(label, header_label, items, denom):
        section_rows.append(len(values)); emit(label)
        colhdr_rows.append(len(values))
        emit(header_label, "#Camps", "Spend (₹)", "% of Total")
        start = len(values)
        for nm, nc, sp in items:
            emit(nm, nc, round(sp), (sp / denom if denom else 0))
        ts = sum(s for _, _, s in items)
        tc = sum(n for _, n, _ in items)
        total_rows.append(len(values)); emit("Total", tc, round(ts), 1.0)
        end = len(values)
        money_ranges.append((start, end, 2))
        pct_ranges.append((start, end, 3))

    stamp = datetime.now(IST).strftime("%d %b %y, %H:%M IST")
    emit("📊 Category-Wise Spend — Portal-wise + Sales/Retarget")
    emit(f"ACTUAL full-day spend for {date} (live budget snapshot not recorded "
         f"for past dates). Backfilled {stamp}")
    emit("")

    grand = sum(c["spend"] for c in camps)
    add_table("ALL PORTALS — CATEGORY SPEND", "Category", cat_rollup(camps), grand)
    emit("")
    add_table("ALL PORTALS — SALES vs RETARGET", "Type", funnel_rollup(camps), grand)
    emit("")
    for portal in PORTAL_ORDER:
        pc = [c for c in camps if c["portal"] == portal]
        if not pc:
            continue
        psp = sum(c["spend"] for c in pc)
        add_table(f"{portal} — CATEGORY SPEND", "Category", cat_rollup(pc), psp)
        emit("")
        add_table(f"{portal} — SALES vs RETARGET", "Type", funnel_rollup(pc), psp)
        emit("")

    ws.resize(rows=len(values) + 10, cols=4)
    ws.clear()
    ws.update(range_name="A1", values=values, value_input_option="USER_ENTERED")

    sid = ws.id

    def fill(r0, r1, color, fg=None, size=None):
        tf = {"bold": True}
        if fg: tf["foregroundColor"] = fg
        if size: tf["fontSize"] = size
        return {"repeatCell": {
            "range": {"sheetId": sid, "startRowIndex": r0, "endRowIndex": r1,
                      "startColumnIndex": 0, "endColumnIndex": 4},
            "cell": {"userEnteredFormat": {"backgroundColor": color, "textFormat": tf}},
            "fields": "userEnteredFormat(backgroundColor,textFormat)"}}

    def numfmt(s, e, col, pattern, ptype="NUMBER"):
        return {"repeatCell": {
            "range": {"sheetId": sid, "startRowIndex": s, "endRowIndex": e,
                      "startColumnIndex": col, "endColumnIndex": col + 1},
            "cell": {"userEnteredFormat": {"numberFormat": {"type": ptype, "pattern": pattern}}},
            "fields": "userEnteredFormat.numberFormat"}}

    fmt = [fill(0, 1, C_TITLE, fg=WHITE, size=13)]
    fmt += [fill(r, r + 1, C_SECTION) for r in section_rows]
    fmt += [fill(r, r + 1, C_COLHDR) for r in colhdr_rows]
    fmt += [fill(r, r + 1, C_TOTAL) for r in total_rows]
    fmt += [numfmt(s, e, c, '"₹"#,##0') for s, e, c in money_ranges]
    fmt += [numfmt(s, e, c, "0.0%", ptype="PERCENT") for s, e, c in pct_ranges]
    fmt.append({"updateSheetProperties": {
        "properties": {"sheetId": sid, "gridProperties": {"frozenRowCount": 2}},
        "fields": "gridProperties.frozenRowCount"}})
    fmt.append({"autoResizeDimensions": {
        "dimensions": {"sheetId": sid, "dimension": "COLUMNS",
                       "startIndex": 0, "endIndex": 4}}})
    sh.batch_update({"requests": fmt})
    return grand


def main():
    conn = db_connect()
    dates = sys.argv[1:]
    sh = open_sheet()
    if not dates:
        # default: every tab that's NOT today and looks like a date
        today = datetime.now(IST).strftime("%Y-%m-%d")
        dates = sorted(w.title for w in sh.worksheets()
                       if len(w.title) == 10 and w.title[4] == '-' and w.title != today)
    print(f"Backfilling spend tabs: {', '.join(dates)}")
    for date in dates:
        camps = collect(conn, date)
        if not camps:
            print(f"  {date}: no spend rows in DB — skipped")
            continue
        grand = write_tab(sh, date, camps)
        print(f"  {date}: {len(camps)} camps, ₹{int(grand):,} spend → written")
    conn.close()


if __name__ == "__main__":
    main()
