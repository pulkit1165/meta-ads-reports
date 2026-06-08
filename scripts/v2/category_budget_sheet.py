#!/usr/bin/env python3
"""
Category-Wise Budget → Google Sheet (daily).

Pulls every currently-ACTIVE campaign across all 3 portals (SM / SML / NBP)
live from the Meta API and writes a fresh date-named tab to the operator's
"CATEGORY WISE BUDGET" sheet. Each tab has:

  • ALL PORTALS — Category budget rollup
  • ALL PORTALS — Sales vs Retarget (budget/day + yesterday's spend)
  • Per-portal (SM, SML, NBP) — the same two tables

Each campaign is classified by:
  - category  → derive_category_v2(name)
  - funnel    → name keyword: 'retarget'/'rtg' = Retarget, 'sales' = Sales,
                else Other

Money model (matches the dashboard's "Aaj ki Nayi Ads" card, NOT raw lifetime):
  - daily-budget campaigns  -> the daily budget as-is
  - lifetime-budget camps   -> lifetime ÷ schedule days  (effective per-day)
  - CBO-off camps           -> sum of the ACTIVE ad-sets' budgets
Summing raw lifetime lump sums inflates the total ~20x, so everything is
normalised to a per-day figure before rolling up.

Spend = yesterday's full-day spend per campaign, pulled live from the Meta
Insights API (so this workflow needs no SQLite DB).

Run by .github/workflows/category-budget.yml on a daily cron. Needs
META_ACCESS_TOKEN (env) and GOOGLE_SERVICE_ACCOUNT_FILE (service account JSON).

Usage:
  python3 scripts/v2/category_budget_sheet.py
"""
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import gspread
from google.oauth2.service_account import Credentials

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _utils import GRAPH_API, IST, PORTAL_ACCOUNTS, meta_get, MetaRateLimitError  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from product_catalogue import derive_category_v2  # noqa: E402

# Operator's "CATEGORY WISE BUDGET" sheet — service account has Editor access.
# One tab per day (tab name = YYYY-MM-DD, IST), so the sheet keeps daily history.
SHEET_ID = "1aZXYLPPqi7LgukH6ixmmy5WnbFz8xLKcwZpdXYwthGY"
PORTAL_ORDER = ["SM", "SML", "NBP"]


# ── budget math ─────────────────────────────────────────────────────────────
def _schedule_days(start_time, stop_time):
    """Whole days between a campaign's start/stop ISO timestamps (min 1), or 0
    if either is missing/unparseable."""
    if not start_time or not stop_time:
        return 0
    try:
        a = datetime.fromisoformat(start_time)
        b = datetime.fromisoformat(stop_time)
    except (ValueError, TypeError):
        return 0
    days = (b - a).days
    return days if days >= 1 else 1


def per_day_budget(c):
    """Effective ₹/day for one campaign dict from the Meta API."""
    d = float(c.get("daily_budget") or 0) / 100
    l = float(c.get("lifetime_budget") or 0) / 100
    if d > 0:
        return d
    if l > 0:
        days = _schedule_days(c.get("start_time"), c.get("stop_time"))
        return l / days if days >= 1 else l
    adsets = ((c.get("adsets") or {}).get("data")) or []
    active = [a for a in adsets if a.get("effective_status") == "ACTIVE"]
    db = sum(float(a.get("daily_budget") or 0) / 100 for a in active)
    if db > 0:
        return db
    return sum(float(a.get("lifetime_budget") or 0) / 100 for a in active)


def funnel_type(name):
    """Sales (prospecting) vs Retarget vs Other, from the campaign name."""
    n = (name or "").lower()
    if "retarget" in n or "rtg" in n or "remarket" in n:
        return "Retarget"
    if "sales" in n:
        return "Sales"
    return "Other"


def campaign_spend(c):
    """Yesterday's spend (₹) for one campaign, from the insights field."""
    ins = (c.get("insights") or {}).get("data") or []
    if not ins:
        return 0.0
    try:
        return float(ins[0].get("spend") or 0)
    except (TypeError, ValueError):
        return 0.0


# ── data collection ──────────────────────────────────────────────────────────
def collect():
    """List of campaign dicts: {portal, category, funnel, budget, spend}."""
    if not os.getenv("META_ACCESS_TOKEN"):
        sys.exit("META_ACCESS_TOKEN not set")

    yday = (datetime.now(IST).date() - timedelta(days=1)).isoformat()
    insights_field = (
        "insights.time_range({'since':'%s','until':'%s'}){spend}" % (yday, yday)
    )

    camps = []
    for portal, accts in PORTAL_ACCOUNTS.items():
        for env_var, friendly in accts:
            aid = os.environ.get(env_var)
            if not aid:
                print(f"  ⚠️  {env_var} not set, skipping", file=sys.stderr)
                continue
            try:
                d = meta_get(
                    f"{GRAPH_API}/{aid}/campaigns",
                    {"fields": "id,name,daily_budget,lifetime_budget,"
                               "start_time,stop_time,"
                               "adsets.limit(50){effective_status,daily_budget,"
                               "lifetime_budget}," + insights_field,
                     "effective_status": '["ACTIVE"]', "limit": 500},
                    max_retries=3,
                )
            except (MetaRateLimitError, Exception) as e:  # noqa: BLE001
                print(f"  ✗ {friendly}: {e}", file=sys.stderr)
                continue
            data = (d or {}).get("data") or []
            n = 0
            for c in data:
                b = per_day_budget(c)
                if b <= 0:
                    continue
                name = c.get("name") or ""
                camps.append({
                    "portal": portal,
                    "category": derive_category_v2(name),
                    "funnel": funnel_type(name),
                    "budget": b,
                    "spend": campaign_spend(c),
                })
                n += 1
            print(f"  {portal} {friendly}: {n} active w/ budget")
            time.sleep(0.25)
    return camps, yday


# ── aggregation ───────────────────────────────────────────────────────────────
def cat_rollup(camps):
    """[(category, n_camps, budget)] sorted by budget desc."""
    agg = defaultdict(lambda: [0, 0.0])
    for c in camps:
        agg[c["category"]][0] += 1
        agg[c["category"]][1] += c["budget"]
    return sorted(((k, v[0], v[1]) for k, v in agg.items()),
                  key=lambda r: -r[2])


def funnel_rollup(camps):
    """[(type, n_camps, budget, spend)] in Sales, Retarget, Other order."""
    agg = {t: [0, 0.0, 0.0] for t in ("Sales", "Retarget", "Other")}
    for c in camps:
        a = agg[c["funnel"]]
        a[0] += 1
        a[1] += c["budget"]
        a[2] += c["spend"]
    return [(t, agg[t][0], agg[t][1], agg[t][2])
            for t in ("Sales", "Retarget", "Other")]


# ── sheet writing ─────────────────────────────────────────────────────────────
def open_sheet():
    sa = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE")
    if not sa or not os.path.isfile(sa):
        sys.exit(f"GOOGLE_SERVICE_ACCOUNT_FILE missing or invalid: {sa}")
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_file(sa, scopes=scopes)
    return gspread.authorize(creds).open_by_key(SHEET_ID)


# Colours
C_TITLE   = {"red": 0.10, "green": 0.46, "blue": 0.95}
C_SECTION = {"red": 0.85, "green": 0.90, "blue": 1.0}
C_COLHDR  = {"red": 0.93, "green": 0.95, "blue": 1.0}
C_TOTAL   = {"red": 0.93, "green": 0.93, "blue": 0.93}
WHITE     = {"red": 1, "green": 1, "blue": 1}


def write_sheet(camps, yday):
    sh = open_sheet()
    title = datetime.now(IST).strftime("%Y-%m-%d")
    titles = {w.title: w for w in sh.worksheets()}
    if title in titles:
        ws = titles[title]
    elif "Sheet1" in titles:
        ws = titles["Sheet1"]
        ws.update_title(title)
    else:
        ws = sh.add_worksheet(title=title, rows=200, cols=4)

    values = []          # list of row lists
    section_rows = []    # 0-based indices of section-header rows
    colhdr_rows = []     # 0-based indices of column-header rows
    total_rows = []      # 0-based indices of Total rows
    money_ranges = []    # (start, end, col) → ₹ format
    pct_ranges = []      # (start, end, col) → % format

    def emit(*cells):
        values.append(list(cells))

    def add_cat_table(label, items, denom):
        section_rows.append(len(values)); emit(label)
        colhdr_rows.append(len(values))
        emit("Category", "#Camps", "Daily Budget (₹/day)", "% of Total")
        start = len(values)
        for cat, nc, bud in items:
            emit(cat, nc, round(bud), (bud / denom if denom else 0))
        tb = sum(b for _, _, b in items)
        tc = sum(n for _, n, _ in items)
        total_rows.append(len(values)); emit("Total", tc, round(tb), 1.0)
        end = len(values)
        money_ranges.append((start, end, 2))
        pct_ranges.append((start, end, 3))

    def add_funnel_table(label, fitems):
        section_rows.append(len(values)); emit(label)
        colhdr_rows.append(len(values))
        emit("Type", "#Camps", "Daily Budget (₹/day)", f"Spent {yday[5:]} (₹)")
        start = len(values)
        for t, nc, bud, sp in fitems:
            emit(t, nc, round(bud), round(sp))
        tb = sum(x[2] for x in fitems)
        tc = sum(x[1] for x in fitems)
        ts = sum(x[3] for x in fitems)
        total_rows.append(len(values)); emit("Total", tc, round(tb), round(ts))
        end = len(values)
        money_ranges.append((start, end, 2))
        money_ranges.append((start, end, 3))

    # ── header ──
    stamp = datetime.now(IST).strftime("%d %b %y, %H:%M IST")
    emit("📊 Category-Wise Budget — Portal-wise + Sales/Retarget")
    emit(f"ACTIVE campaigns, live from Meta. Budget = effective ₹/day. "
         f"Spend = full-day spend for {yday}. Refreshed {stamp}")
    emit("")

    grand_budget = sum(c["budget"] for c in camps)
    add_cat_table("ALL PORTALS — CATEGORY BUDGET", cat_rollup(camps), grand_budget)
    emit("")
    add_funnel_table("ALL PORTALS — SALES vs RETARGET", funnel_rollup(camps))
    emit("")

    for portal in PORTAL_ORDER:
        pc = [c for c in camps if c["portal"] == portal]
        if not pc:
            continue
        pbud = sum(c["budget"] for c in pc)
        add_cat_table(f"{portal} — CATEGORY BUDGET", cat_rollup(pc), pbud)
        emit("")
        add_funnel_table(f"{portal} — SALES vs RETARGET", funnel_rollup(pc))
        emit("")

    # ── push values ──
    ws.resize(rows=len(values) + 10, cols=4)
    ws.clear()
    ws.update(range_name="A1", values=values, value_input_option="USER_ENTERED")

    # ── formatting ──
    sid = ws.id

    def fill(r0, r1, color, bold=True, fg=None, size=None):
        tf = {"bold": bold}
        if fg:
            tf["foregroundColor"] = fg
        if size:
            tf["fontSize"] = size
        return {"repeatCell": {
            "range": {"sheetId": sid, "startRowIndex": r0, "endRowIndex": r1,
                      "startColumnIndex": 0, "endColumnIndex": 4},
            "cell": {"userEnteredFormat": {
                "backgroundColor": color, "textFormat": tf}},
            "fields": "userEnteredFormat(backgroundColor,textFormat)"}}

    def numfmt(start, end, col, pattern, ptype="NUMBER"):
        return {"repeatCell": {
            "range": {"sheetId": sid, "startRowIndex": start, "endRowIndex": end,
                      "startColumnIndex": col, "endColumnIndex": col + 1},
            "cell": {"userEnteredFormat": {
                "numberFormat": {"type": ptype, "pattern": pattern}}},
            "fields": "userEnteredFormat.numberFormat"}}

    fmt = [fill(0, 1, C_TITLE, fg=WHITE, size=13)]
    for r in section_rows:
        fmt.append(fill(r, r + 1, C_SECTION))
    for r in colhdr_rows:
        fmt.append(fill(r, r + 1, C_COLHDR))
    for r in total_rows:
        fmt.append(fill(r, r + 1, C_TOTAL))
    for start, end, col in money_ranges:
        fmt.append(numfmt(start, end, col, '"₹"#,##0'))
    for start, end, col in pct_ranges:
        fmt.append(numfmt(start, end, col, "0.0%", ptype="PERCENT"))
    fmt.append({"updateSheetProperties": {
        "properties": {"sheetId": sid, "gridProperties": {"frozenRowCount": 2}},
        "fields": "gridProperties.frozenRowCount"}})
    fmt.append({"autoResizeDimensions": {
        "dimensions": {"sheetId": sid, "dimension": "COLUMNS",
                       "startIndex": 0, "endIndex": 4}}})
    sh.batch_update({"requests": fmt})
    return grand_budget, title


def main():
    print(f"Category-Wise Budget → Sheet — "
          f"{datetime.now(IST).strftime('%d %b %y %H:%M IST')}")
    camps, yday = collect()
    if not camps:
        sys.exit("No active campaigns with budget found — not writing sheet.")
    grand, title = write_sheet(camps, yday)
    tot_spend = sum(c["spend"] for c in camps)
    print()
    print(f"  {len(camps)} active campaigns  |  ₹{int(grand):,}/day budget  "
          f"|  ₹{int(tot_spend):,} spent {yday}")
    print(f"  Written to sheet {SHEET_ID} (tab {title})")


if __name__ == "__main__":
    main()
