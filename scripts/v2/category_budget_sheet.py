#!/usr/bin/env python3
"""
Category-Wise Budget → Google Sheet (daily).

Pulls every currently-ACTIVE campaign across all 3 portals (SM / SML / NBP)
live from the Meta API, classifies each by NTN category via
derive_category_v2(), rolls up the effective per-day budget, and overwrites a
single tab in the operator's "CATEGORY WISE BUDGET" sheet.

Money model (matches the dashboard's "Aaj ki Nayi Ads" card, NOT raw lifetime):
  - daily-budget campaigns  -> the daily budget as-is
  - lifetime-budget camps   -> lifetime ÷ schedule days  (effective per-day)
  - CBO-off camps           -> sum of the ACTIVE ad-sets' budgets
Summing raw lifetime lump sums inflates the total ~20x, so everything is
normalised to a per-day figure before rolling up.

Run by .github/workflows/category-budget.yml on a daily cron. Needs
META_ACCESS_TOKEN (env) and GOOGLE_SERVICE_ACCOUNT_FILE (service account JSON).
The service account antriksh-bot@antriksh-meta-reports.iam.gserviceaccount.com
already has Editor access on the sheet.

Usage:
  python3 scripts/v2/category_budget_sheet.py
"""
import os
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import gspread
from google.oauth2.service_account import Credentials

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _utils import GRAPH_API, IST, PORTAL_ACCOUNTS, meta_get, MetaRateLimitError  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from product_catalogue import derive_category_v2  # noqa: E402

# Operator's "CATEGORY WISE BUDGET" sheet — service account has Editor access.
SHEET_ID = "1aZXYLPPqi7LgukH6ixmmy5WnbFz8xLKcwZpdXYwthGY"
TAB = "Sheet1"


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


# ── data collection ──────────────────────────────────────────────────────────
def collect():
    """category -> {'camps': int, 'budget': float}  across all active camps."""
    if not os.getenv("META_ACCESS_TOKEN"):
        sys.exit("META_ACCESS_TOKEN not set")

    by_cat = defaultdict(lambda: {"camps": 0, "budget": 0.0})
    total_camps = 0
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
                               "lifetime_budget}",
                     "effective_status": '["ACTIVE"]', "limit": 500},
                    max_retries=3,
                )
            except (MetaRateLimitError, Exception) as e:  # noqa: BLE001
                print(f"  ✗ {friendly}: {e}", file=sys.stderr)
                continue
            camps = (d or {}).get("data") or []
            n = 0
            for c in camps:
                b = per_day_budget(c)
                if b <= 0:
                    continue
                cat = derive_category_v2(c.get("name") or "")
                by_cat[cat]["camps"] += 1
                by_cat[cat]["budget"] += b
                n += 1
            total_camps += n
            print(f"  {portal} {friendly}: {n} active w/ budget")
            time.sleep(0.25)
    return by_cat, total_camps


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


def write_sheet(by_cat, total_camps):
    sh = open_sheet()
    try:
        ws = sh.worksheet(TAB)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=TAB, rows=50, cols=4)

    grand = sum(v["budget"] for v in by_cat.values())
    rows_sorted = sorted(by_cat.items(), key=lambda kv: -kv[1]["budget"])
    stamp = datetime.now(IST).strftime("%d %b %y, %H:%M IST")

    values = []
    values.append(["📊 Category-Wise Budget"])
    values.append([f"Currently ACTIVE campaigns, all portals (SM/SML/NBP). "
                   f"Budget = effective ₹/day. Refreshed {stamp}"])
    values.append([])
    values.append(["Category", "#Camps", "Daily Budget (₹/day)", "% of Total"])
    for cat, v in rows_sorted:
        share = round(v["budget"] / grand * 100, 1) if grand else 0
        values.append([cat, v["camps"], round(v["budget"]), share / 100])
    values.append(["Grand Total", total_camps, round(grand), 1.0])

    ws.clear()
    ws.update(range_name="A1", values=values, value_input_option="USER_ENTERED")

    # Formatting
    sid = ws.id
    n_rows = len(values)
    last = n_rows  # 1-based row index of the Grand Total row
    fmt = [
        # Title
        {"repeatCell": {
            "range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": 1,
                      "startColumnIndex": 0, "endColumnIndex": 4},
            "cell": {"userEnteredFormat": {
                "backgroundColor": {"red": 0.10, "green": 0.46, "blue": 0.95},
                "textFormat": {"bold": True, "fontSize": 13,
                               "foregroundColor": {"red": 1, "green": 1, "blue": 1}}}},
            "fields": "userEnteredFormat(backgroundColor,textFormat)"}},
        # Header row (row 4 -> index 3)
        {"repeatCell": {
            "range": {"sheetId": sid, "startRowIndex": 3, "endRowIndex": 4,
                      "startColumnIndex": 0, "endColumnIndex": 4},
            "cell": {"userEnteredFormat": {
                "backgroundColor": {"red": 0.85, "green": 0.90, "blue": 1.0},
                "textFormat": {"bold": True}}},
            "fields": "userEnteredFormat(backgroundColor,textFormat)"}},
        # Budget column money format (rows 5..last)
        {"repeatCell": {
            "range": {"sheetId": sid, "startRowIndex": 4, "endRowIndex": last,
                      "startColumnIndex": 2, "endColumnIndex": 3},
            "cell": {"userEnteredFormat": {"numberFormat": {
                "type": "NUMBER", "pattern": "#,##0"}}},
            "fields": "userEnteredFormat.numberFormat"}},
        # % column
        {"repeatCell": {
            "range": {"sheetId": sid, "startRowIndex": 4, "endRowIndex": last,
                      "startColumnIndex": 3, "endColumnIndex": 4},
            "cell": {"userEnteredFormat": {"numberFormat": {
                "type": "PERCENT", "pattern": "0.0%"}}},
            "fields": "userEnteredFormat.numberFormat"}},
        # Grand Total row
        {"repeatCell": {
            "range": {"sheetId": sid, "startRowIndex": last - 1, "endRowIndex": last,
                      "startColumnIndex": 0, "endColumnIndex": 4},
            "cell": {"userEnteredFormat": {
                "backgroundColor": {"red": 0.91, "green": 0.95, "blue": 1.0},
                "textFormat": {"bold": True}}},
            "fields": "userEnteredFormat(backgroundColor,textFormat)"}},
        {"updateSheetProperties": {
            "properties": {"sheetId": sid,
                           "gridProperties": {"frozenRowCount": 4}},
            "fields": "gridProperties.frozenRowCount"}},
        {"autoResizeDimensions": {
            "dimensions": {"sheetId": sid, "dimension": "COLUMNS",
                           "startIndex": 0, "endIndex": 4}}},
    ]
    sh.batch_update({"requests": fmt})
    return grand


def main():
    print(f"Category-Wise Budget → Sheet — "
          f"{datetime.now(IST).strftime('%d %b %y %H:%M IST')}")
    by_cat, total_camps = collect()
    if not by_cat:
        sys.exit("No active campaigns with budget found — not writing sheet.")
    grand = write_sheet(by_cat, total_camps)
    print()
    print(f"  {total_camps} active campaigns  |  ₹{int(grand):,}/day total  "
          f"|  {len(by_cat)} categories")
    print(f"  Written to sheet {SHEET_ID} (tab {TAB})")


if __name__ == "__main__":
    main()
