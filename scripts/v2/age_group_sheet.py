#!/usr/bin/env python3
"""
Age-Group Performance → Google Sheet (daily, 12 PM IST).

Pulls MONTH-TO-DATE account-level Meta insights for all 3 portals (SM / SML /
NBP) broken down by age bracket, and writes one row per age group with Spend,
Purchases, Revenue and ROAS to a new tab in the operator's sheet
(18shZsLz…, the same sheet as the top-states report).

One tab per day, tab name = `Age YYYY-MM-DD` (IST), so it keeps daily history
and never collides with the state-report tabs (`YYYY-MM-DD`).

Money/conversion model matches the rest of the repo:
  purchases = sum(actions where action_type in {omni_purchase, purchase})
  revenue   = sum(action_values where action_type in {omni_purchase, purchase})
  ROAS      = revenue / spend

Run by .github/workflows/age-group.yml on a daily cron. Needs META_ACCESS_TOKEN
(env) and GOOGLE_SERVICE_ACCOUNT_FILE (service account JSON). The service account
antriksh-bot@antriksh-meta-reports.iam.gserviceaccount.com must have Editor
access on the sheet.

Usage:
  python3 scripts/v2/age_group_sheet.py
"""
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import gspread
from google.oauth2.service_account import Credentials

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _utils import GRAPH_API, IST, PORTAL_ACCOUNTS, meta_paginate  # noqa: E402

# Same sheet as the top-states report; age tabs are prefixed so they don't clash.
SHEET_ID = "18shZsLzcI6NEUfJFUH3ZNUm6Za8a7aNegea0T2VyZSc"

PURCHASE_TYPES = {"omni_purchase", "purchase"}

# Canonical display order for Meta's age brackets.
AGE_ORDER = ["13-17", "18-24", "25-34", "35-44", "45-54", "55-64", "65+", "unknown"]


def _safe_float(x, default=0.0):
    try:
        return float(x or 0)
    except (TypeError, ValueError):
        return default


def _action_sum(actions, types):
    if not actions:
        return 0.0
    return sum(_safe_float(a.get("value")) for a in actions
              if a.get("action_type") in types)


# ── data collection ──────────────────────────────────────────────────────────
def collect(since, until):
    """age_bracket -> {'spend','purchases','revenue'} summed across all accounts."""
    if not os.getenv("META_ACCESS_TOKEN"):
        sys.exit("META_ACCESS_TOKEN not set")

    by_age = defaultdict(lambda: {"spend": 0.0, "purchases": 0.0, "revenue": 0.0})
    for portal, accts in PORTAL_ACCOUNTS.items():
        for env_var, friendly in accts:
            aid = os.environ.get(env_var)
            if not aid:
                print(f"  ⚠️  {env_var} not set, skipping", file=sys.stderr)
                continue
            rows = meta_paginate(
                f"{GRAPH_API}/{aid}/insights",
                {"level": "account",
                 "breakdowns": "age",
                 "fields": "spend,actions,action_values",
                 "time_range": json.dumps({"since": since, "until": until}),
                 "limit": 500},
            )
            n = 0
            for r in rows:
                age = r.get("age") or "unknown"
                by_age[age]["spend"]     += _safe_float(r.get("spend"))
                by_age[age]["purchases"] += _action_sum(r.get("actions"), PURCHASE_TYPES)
                by_age[age]["revenue"]   += _action_sum(r.get("action_values"), PURCHASE_TYPES)
                n += 1
            print(f"  {portal} {friendly}: {n} age rows")
            time.sleep(0.25)
    return by_age


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


def _ordered_ages(by_age):
    """Known brackets in canonical order first, then any unexpected ones."""
    known = [a for a in AGE_ORDER if a in by_age]
    extra = sorted(a for a in by_age if a not in AGE_ORDER)
    return known + extra


def write_sheet(by_age, since, until):
    sh = open_sheet()
    title = "Age " + datetime.now(IST).strftime("%Y-%m-%d")
    titles = {w.title: w for w in sh.worksheets()}
    if title in titles:
        ws = titles[title]                       # re-run same day → refresh it
    else:
        ws = sh.add_worksheet(title=title, rows=30, cols=6)

    tot_spend = sum(v["spend"] for v in by_age.values())
    tot_purch = sum(v["purchases"] for v in by_age.values())
    tot_rev   = sum(v["revenue"] for v in by_age.values())
    stamp = datetime.now(IST).strftime("%d %b %y, %H:%M IST")

    values = []
    values.append(["📊 Age-Group Performance — Meta Ads"])
    values.append([f"Month-to-date {since} → {until}, all portals (SM/SML/NBP). "
                   f"Refreshed {stamp}"])
    values.append([])
    values.append(["Age Group", "Spend", "Purchases", "Revenue", "ROAS", "% Purch"])
    for age in _ordered_ages(by_age):
        v = by_age[age]
        roas = v["revenue"] / v["spend"] if v["spend"] else 0.0
        share = v["purchases"] / tot_purch if tot_purch else 0.0
        values.append([age, round(v["spend"]), round(v["purchases"]),
                       round(v["revenue"]), round(roas, 2), share])
    tot_roas = tot_rev / tot_spend if tot_spend else 0.0
    values.append(["TOTAL", round(tot_spend), round(tot_purch),
                   round(tot_rev), round(tot_roas, 2), 1.0 if tot_purch else 0.0])

    ws.clear()
    ws.update(range_name="A1", values=values, value_input_option="USER_ENTERED")

    sid = ws.id
    n_rows = len(values)
    last = n_rows  # 1-based index of TOTAL row
    fmt = [
        # Title banner
        {"repeatCell": {
            "range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": 1,
                      "startColumnIndex": 0, "endColumnIndex": 6},
            "cell": {"userEnteredFormat": {
                "backgroundColor": {"red": 0.12, "green": 0.31, "blue": 0.47},
                "textFormat": {"bold": True, "fontSize": 13,
                               "foregroundColor": {"red": 1, "green": 1, "blue": 1}}}},
            "fields": "userEnteredFormat(backgroundColor,textFormat)"}},
        # Header row (row 4 -> index 3)
        {"repeatCell": {
            "range": {"sheetId": sid, "startRowIndex": 3, "endRowIndex": 4,
                      "startColumnIndex": 0, "endColumnIndex": 6},
            "cell": {"userEnteredFormat": {
                "backgroundColor": {"red": 1.0, "green": 0.95, "blue": 0.80},
                "textFormat": {"bold": True}}},
            "fields": "userEnteredFormat(backgroundColor,textFormat)"}},
        # Money columns (Spend, Revenue) — cols B,D = idx 1,3
        {"repeatCell": {
            "range": {"sheetId": sid, "startRowIndex": 4, "endRowIndex": last,
                      "startColumnIndex": 1, "endColumnIndex": 2},
            "cell": {"userEnteredFormat": {"numberFormat": {
                "type": "NUMBER", "pattern": "#,##0"}}},
            "fields": "userEnteredFormat.numberFormat"}},
        {"repeatCell": {
            "range": {"sheetId": sid, "startRowIndex": 4, "endRowIndex": last,
                      "startColumnIndex": 3, "endColumnIndex": 4},
            "cell": {"userEnteredFormat": {"numberFormat": {
                "type": "NUMBER", "pattern": "#,##0"}}},
            "fields": "userEnteredFormat.numberFormat"}},
        # ROAS column (E -> idx 4)
        {"repeatCell": {
            "range": {"sheetId": sid, "startRowIndex": 4, "endRowIndex": last,
                      "startColumnIndex": 4, "endColumnIndex": 5},
            "cell": {"userEnteredFormat": {"numberFormat": {
                "type": "NUMBER", "pattern": "0.00\"x\""}}},
            "fields": "userEnteredFormat.numberFormat"}},
        # % Purch column (F -> idx 5)
        {"repeatCell": {
            "range": {"sheetId": sid, "startRowIndex": 4, "endRowIndex": last,
                      "startColumnIndex": 5, "endColumnIndex": 6},
            "cell": {"userEnteredFormat": {"numberFormat": {
                "type": "PERCENT", "pattern": "0.0%"}}},
            "fields": "userEnteredFormat.numberFormat"}},
        # TOTAL row
        {"repeatCell": {
            "range": {"sheetId": sid, "startRowIndex": last - 1, "endRowIndex": last,
                      "startColumnIndex": 0, "endColumnIndex": 6},
            "cell": {"userEnteredFormat": {
                "backgroundColor": {"red": 0.91, "green": 0.91, "blue": 0.91},
                "textFormat": {"bold": True}}},
            "fields": "userEnteredFormat(backgroundColor,textFormat)"}},
        {"updateSheetProperties": {
            "properties": {"sheetId": sid,
                           "gridProperties": {"frozenRowCount": 4}},
            "fields": "gridProperties.frozenRowCount"}},
        {"autoResizeDimensions": {
            "dimensions": {"sheetId": sid, "dimension": "COLUMNS",
                           "startIndex": 0, "endIndex": 6}}},
    ]
    sh.batch_update({"requests": fmt})
    return tot_spend, tot_purch, tot_rev, title


def main():
    now = datetime.now(IST)
    since = now.replace(day=1).strftime("%Y-%m-%d")
    until = now.strftime("%Y-%m-%d")
    print(f"Age-Group Performance -> Sheet - month-to-date {since} to {until} "
          f"({now.strftime('%d %b %y %H:%M IST')})")
    by_age = collect(since, until)
    if not by_age:
        sys.exit("No age-broken-down insights returned — not writing sheet.")
    tot_spend, tot_purch, tot_rev, title = write_sheet(by_age, since, until)
    roas = tot_rev / tot_spend if tot_spend else 0
    print()
    print(f"  {len(by_age)} age groups  |  ₹{int(tot_spend):,} spend  "
          f"|  {int(tot_purch):,} purchases  |  {roas:.2f}x ROAS")
    print(f"  Written to sheet {SHEET_ID} (tab {title})")


if __name__ == "__main__":
    main()
