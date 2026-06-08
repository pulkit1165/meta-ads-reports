#!/usr/bin/env python3
"""
Home Decor (Crystal Home Decor) → Google Sheet (daily, 12 PM IST).

Pulls every currently-ACTIVE campaign across all 3 portals (SM / SML / NBP) live
from the Meta API, keeps ONLY the home-decor items — frames, miniatures, plates,
clocks, idols, geodes, horses, etc. (derive_category_v2 == 'Crystal Home Decor';
this excludes bracelets / keyrings / sutras, which are 'Crystal Accessory') —
groups them by product, and writes yesterday's budget / spend / ROAS to the
operator's "home decor" sheet.

For each product it reports:
  #Camps | Daily Budget (₹/day) | Spend yesterday (₹) | ROAS yesterday | Verdict
plus a KPI strip (total budget, total spend, campaigns running, blended ROAS).

Window: "kal" = yesterday (IST), single day, 7d_click attribution (matches the
rest of the pipeline). History: one new tab per day, named by the DATA date
(yesterday, YYYY-MM-DD), so the workbook keeps a daily history.

Run by .github/workflows/home-decor.yml on the 06:30 UTC = 12:00 IST cron. Needs
META_ACCESS_TOKEN (env) and GOOGLE_SERVICE_ACCOUNT_FILE (service account JSON).
The service account antriksh-bot@antriksh-meta-reports.iam.gserviceaccount.com
must have Editor access on the sheet (operator shares it manually once).

Usage:
  python3 scripts/v2/home_decor_sheet.py
"""
import os
import re
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
# classify_corrected applies the catalogue's wanda/astro precedence fixes, so a
# "wanda_loose_neck_bright" skin camp is NOT mislabelled crystal. We require its
# product-level category == 'Crystal' on top of the v2 'Crystal Home Decor' tag.
from active_budget_by_product import classify_corrected  # noqa: E402

# Operator's "home decor" sheet — service account must have Editor access.
SHEET_ID = "1X0FDfiS5ZflJykVZcsFgieI0qHrWC4OOuO3jMBfB6K0"

# Only this category counts as "home decor" (frames / miniatures / plates /
# clocks / idols / geodes / horses …). 'Crystal Accessory' (bracelets, keyrings,
# sutras) is intentionally excluded.
HOME_DECOR_CATEGORY = "Crystal Home Decor"

# SM_CREDIT_LINE_06 is intentionally skipped (matches active_budget_by_product.py):
# it carries dozens of tiny-budget, zero-spend test/leftover camps that pollute
# the rollup. Drop this entry to include it.
EXCLUDE_ACCOUNTS = {"SM_CREDIT_LINE_06"}

# Canonical home-decor product list (decor items from product_catalogue's Crystal
# rules, excluding body-worn accessories — bracelets / sutras). Every product here
# is always shown, even with no active camp yesterday (renders as 0 / "💤 Off"), so
# the operator sees the full home-decor range each day. Any live product not listed
# is appended automatically.
HOME_DECOR_PRODUCTS = [
    "Peacock Frame", "Crystal Frame", "7 Horses / Richie Rich",
    "Sunflower Selenite Plate", "Selenite Products", "Selenite Coaster",
    "Crystal Coaster", "Crystal Clock", "Crystal Hourglass",
    "Money Bowl with Geode", "Crystal Miniature Series", "Crystal Deer Plate",
    "Crystal Owl", "Hanuman Crystal", "Ganesha Crystal", "Money Magnet Crystal",
    "Rose Quartz Crystal", "Sleek Crystal", "Pyrite Products", "Crystal Mix",
    "Crystal Products",
]


def excluded(name):
    """Mirror the operator's category reports: drop BID-tagged and bau_/meta_test_ camps."""
    n = (name or "").lower()
    if "bau_" in n or "meta_test_" in n:
        return True
    if "bid" in re.split(r"[^a-z0-9]+", n):
        return True
    return False


# ── budget math (effective ₹/day, same model as category_budget_sheet) ────────
def _schedule_days(start_time, stop_time):
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


# ── yesterday spend + ROAS (7d_click attribution) ────────────────────────────
def _extract_roas(pr_field):
    if not pr_field:
        return 0.0
    for it in pr_field:
        if isinstance(it, dict):
            v = it.get("7d_click") or it.get("value", 0)
            try:
                return float(v or 0)
            except (TypeError, ValueError):
                return 0.0
    return 0.0


def fetch_perf_yesterday(cids, yday):
    """cid -> {'spend': float, 'roas': float} for the single day `yday` (IST)."""
    out = {}
    for i in range(0, len(cids), 50):
        batch = cids[i:i + 50]
        d = meta_get(f"{GRAPH_API}/", {
            "ids": ",".join(batch),
            "fields": (
                f"insights.time_range({{'since':'{yday}','until':'{yday}'}})"
                f".action_attribution_windows(['7d_click']){{spend,purchase_roas}}"
            ),
        })
        for cid, obj in (d or {}).items():
            if not isinstance(obj, dict) or "error" in obj:
                out[cid] = {"spend": 0.0, "roas": 0.0}
                continue
            ins = (obj.get("insights") or {}).get("data", [])
            if not ins:
                out[cid] = {"spend": 0.0, "roas": 0.0}
                continue
            row = ins[0]
            try:
                spend = float(row.get("spend", 0) or 0)
            except (TypeError, ValueError):
                spend = 0.0
            out[cid] = {"spend": spend, "roas": _extract_roas(row.get("purchase_roas"))}
        time.sleep(0.5)
    return out


# ── data collection ───────────────────────────────────────────────────────────
def collect():
    """product -> {'camps': [ {cid,name,portal,budget,spend,roas} ]}, all home-decor only."""
    if not os.getenv("META_ACCESS_TOKEN"):
        sys.exit("META_ACCESS_TOKEN not set")

    by_product = defaultdict(list)
    all_cids = []

    for portal, accts in PORTAL_ACCOUNTS.items():
        for env_var, friendly in accts:
            if env_var in EXCLUDE_ACCOUNTS:
                continue
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
                name = c.get("name") or ""
                if excluded(name):
                    continue
                if derive_category_v2(name) != HOME_DECOR_CATEGORY:
                    continue
                product, corr_cat = classify_corrected(name)
                if corr_cat != "Crystal":   # guards against wanda-misclassified skin/jewellery
                    continue
                budget = per_day_budget(c)
                if budget <= 0:
                    continue
                by_product[product].append({
                    "cid": c["id"], "name": name, "portal": portal,
                    "budget": budget, "spend": 0.0, "roas": 0.0,
                })
                all_cids.append(c["id"])
                n += 1
            print(f"  {portal} {friendly}: {n} home-decor active w/ budget")
            time.sleep(0.25)

    # Yesterday's spend + ROAS for every home-decor camp
    yday = (datetime.now(IST).date() - timedelta(days=1)).isoformat()
    cids = list({c["cid"] for camps in by_product.values() for c in camps})
    print(f"Fetching {yday} spend + ROAS for {len(cids)} home-decor camps...")
    perf = fetch_perf_yesterday(cids, yday)
    for camps in by_product.values():
        for c in camps:
            p = perf.get(c["cid"], {})
            c["spend"] = p.get("spend", 0.0)
            c["roas"] = p.get("roas", 0.0)

    return by_product, yday


def _verdict(roas):
    if roas >= 1.5:
        return "⭐ Scale"
    if roas >= 1.0:
        return "✓ Healthy"
    if roas >= 0.8:
        return "~ Watch"
    return "⚠ Cut"


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


def write_sheet(by_product, yday):
    sh = open_sheet()
    title = yday  # tab name = the DATA date (yesterday), keeps daily history
    titles = {w.title: w for w in sh.worksheets()}
    if title in titles:
        ws = titles[title]
    elif "Sheet1" in titles:
        ws = titles["Sheet1"]
        ws.update_title(title)
    else:
        ws = sh.add_worksheet(title=title, rows=80, cols=7)

    # Roll up per product. Seed with the full canonical list so inactive products
    # still show (as 0), then add any live product not already listed.
    order = {p: i for i, p in enumerate(HOME_DECOR_PRODUCTS)}
    products = list(HOME_DECOR_PRODUCTS) + [
        p for p in by_product if p not in order]
    prod_rows = []
    for product in products:
        camps = by_product.get(product, [])
        budget = sum(c["budget"] for c in camps)
        spend = sum(c["spend"] for c in camps)
        rev = sum(c["spend"] * c["roas"] for c in camps)
        roas = round(rev / spend, 2) if spend else 0.0
        prod_rows.append((product, len(camps), budget, spend, roas))
    # Active (has budget) first by budget desc; inactive (0) after, in list order.
    prod_rows.sort(key=lambda r: (-r[2], -r[3], order.get(r[0], 999)))

    tot_camps = sum(r[1] for r in prod_rows)
    tot_budget = sum(r[2] for r in prod_rows)
    tot_spend = sum(r[3] for r in prod_rows)
    tot_rev = sum(c["spend"] * c["roas"] for camps in by_product.values() for c in camps)
    tot_roas = round(tot_rev / tot_spend, 2) if tot_spend else 0.0

    stamp = datetime.now(IST).strftime("%d %b %y, %H:%M IST")
    yday_label = datetime.fromisoformat(yday).strftime("%d %b %Y")

    values = []
    values.append(["🏠 Home Decor — Daily Report"])
    values.append([f"Crystal Home Decor only (frames / miniatures / plates / clocks / "
                   f"idols / geodes). Data: {yday_label} (yesterday). Refreshed {stamp}"])
    values.append([])
    # KPI strip
    values.append(["Total Budget (₹/day)", "Spend (yesterday ₹)", "Campaigns Running", "ROAS"])
    values.append([round(tot_budget), round(tot_spend), tot_camps, tot_roas])
    values.append([])
    # Product table
    values.append(["#", "Product", "#Camps", "Budget (₹/day)", "Spend (₹)", "ROAS", "Verdict"])
    for i, (product, ncamps, budget, spend, roas) in enumerate(prod_rows, 1):
        verdict = _verdict(roas) if ncamps else "💤 Off"
        values.append([i, product, ncamps, round(budget), round(spend), roas, verdict])
    values.append(["", "── TOTAL ──", tot_camps, round(tot_budget), round(tot_spend),
                   tot_roas, ""])

    ws.clear()
    ws.update(range_name="A1", values=values, value_input_option="USER_ENTERED")

    # ── formatting ──
    sid = ws.id
    kpi_hdr = 4          # 1-based row of KPI header
    kpi_val = 5
    tbl_hdr = 7          # 1-based row of product-table header
    tbl_first = tbl_hdr + 1
    total_row = len(values)
    blue = {"red": 0.12, "green": 0.22, "blue": 0.39}
    lightblue = {"red": 0.85, "green": 0.88, "blue": 0.95}

    def cell_fmt(r0, r1, c0, c1, body, fields):
        return {"repeatCell": {
            "range": {"sheetId": sid, "startRowIndex": r0, "endRowIndex": r1,
                      "startColumnIndex": c0, "endColumnIndex": c1},
            "cell": {"userEnteredFormat": body}, "fields": fields}}

    money = {"numberFormat": {"type": "NUMBER", "pattern": "\"₹\"#,##0"}}
    roasf = {"numberFormat": {"type": "NUMBER", "pattern": "0.00\"x\""}}

    fmt = [
        # Title
        cell_fmt(0, 1, 0, 7,
                 {"backgroundColor": blue,
                  "textFormat": {"bold": True, "fontSize": 13,
                                 "foregroundColor": {"red": 1, "green": 1, "blue": 1}}},
                 "userEnteredFormat(backgroundColor,textFormat)"),
        # KPI header + values
        cell_fmt(kpi_hdr - 1, kpi_hdr, 0, 4,
                 {"backgroundColor": lightblue, "textFormat": {"bold": True},
                  "horizontalAlignment": "CENTER"},
                 "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment)"),
        cell_fmt(kpi_val - 1, kpi_val, 0, 4,
                 {"textFormat": {"bold": True, "fontSize": 12},
                  "horizontalAlignment": "CENTER"},
                 "userEnteredFormat(textFormat,horizontalAlignment)"),
        cell_fmt(kpi_val - 1, kpi_val, 0, 2, money, "userEnteredFormat.numberFormat"),
        cell_fmt(kpi_val - 1, kpi_val, 3, 4, roasf, "userEnteredFormat.numberFormat"),
        # Product-table header
        cell_fmt(tbl_hdr - 1, tbl_hdr, 0, 7,
                 {"backgroundColor": blue,
                  "textFormat": {"bold": True,
                                 "foregroundColor": {"red": 1, "green": 1, "blue": 1}}},
                 "userEnteredFormat(backgroundColor,textFormat)"),
        # Budget + Spend money cols (D,E) for table rows incl total
        cell_fmt(tbl_first - 1, total_row, 3, 5, money, "userEnteredFormat.numberFormat"),
        # ROAS col (F)
        cell_fmt(tbl_first - 1, total_row, 5, 6, roasf, "userEnteredFormat.numberFormat"),
        # Total row
        cell_fmt(total_row - 1, total_row, 0, 7,
                 {"backgroundColor": lightblue, "textFormat": {"bold": True}},
                 "userEnteredFormat(backgroundColor,textFormat)"),
        {"updateSheetProperties": {
            "properties": {"sheetId": sid, "gridProperties": {"frozenRowCount": tbl_hdr}},
            "fields": "gridProperties.frozenRowCount"}},
        {"autoResizeDimensions": {
            "dimensions": {"sheetId": sid, "dimension": "COLUMNS",
                           "startIndex": 0, "endIndex": 7}}},
    ]
    sh.batch_update({"requests": fmt})
    return title, tot_camps, tot_budget, tot_spend, tot_roas


def main():
    print(f"Home Decor → Sheet — {datetime.now(IST).strftime('%d %b %y %H:%M IST')}")
    by_product, yday = collect()
    if not by_product:
        sys.exit("No active home-decor campaigns with budget found — not writing sheet.")
    title, n, budget, spend, roas = write_sheet(by_product, yday)
    print()
    print(f"  {n} home-decor camps  |  ₹{int(budget):,}/day budget  |  "
          f"₹{int(spend):,} spend ({yday})  |  {roas}x ROAS")
    print(f"  Written to sheet {SHEET_ID} (tab {title})")


if __name__ == "__main__":
    main()
