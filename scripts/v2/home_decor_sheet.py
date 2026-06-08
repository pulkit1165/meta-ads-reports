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

Run by .github/workflows/home-decor.yml on the 01:30 UTC = 07:00 IST cron. Needs
META_ACCESS_TOKEN (env) and GOOGLE_SERVICE_ACCOUNT_FILE (service account JSON).
The service account antriksh-bot@antriksh-meta-reports.iam.gserviceaccount.com
must have Editor access on the sheet (operator shares it manually once).

Usage:
  python3 scripts/v2/home_decor_sheet.py
"""
import json
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
from _utils import GRAPH_API, IST, PORTAL_ACCOUNTS, meta_get, meta_paginate, MetaRateLimitError  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from product_catalogue import derive_category_v2  # noqa: E402
# classify_corrected applies the catalogue's wanda/astro precedence fixes, so a
# "wanda_loose_neck_bright" skin camp is NOT mislabelled crystal. We require its
# product-level category == 'Crystal' on top of the v2 'Crystal Home Decor' tag.
from active_budget_by_product import classify_corrected  # noqa: E402

# Shopify daily store sales (real orders / gross / net, all 3 stores) — fail-safe.
from shopify_daily_sales import store_sales  # noqa: E402

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

# Min yesterday spend (₹) for an ad to qualify for the top/worst-creative ranking
# — keeps a ₹40-spend fluke off the leaderboard.
MIN_CREATIVE_SPEND = 500


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


def _extract_action(field, types=("omni_purchase", "purchase")):
    """Sum the 7d_click value of purchase actions/action_values."""
    if not field:
        return 0.0
    for it in field:
        if isinstance(it, dict) and it.get("action_type") in types:
            v = it.get("7d_click") or it.get("value", 0)
            try:
                return float(v or 0)
            except (TypeError, ValueError):
                return 0.0
    return 0.0


def fetch_perf_yesterday(cids, yday):
    """cid -> {'spend','roas','orders','revenue'} for the single day `yday` (IST).

    orders  = ad-attributed purchases (7d click), revenue = purchase value (₹).
    """
    out = {}
    for i in range(0, len(cids), 50):
        batch = cids[i:i + 50]
        d = meta_get(f"{GRAPH_API}/", {
            "ids": ",".join(batch),
            "fields": (
                f"insights.time_range({{'since':'{yday}','until':'{yday}'}})"
                f".action_attribution_windows(['7d_click'])"
                f"{{spend,purchase_roas,actions,action_values}}"
            ),
        })
        for cid, obj in (d or {}).items():
            if not isinstance(obj, dict) or "error" in obj:
                out[cid] = {"spend": 0.0, "roas": 0.0, "orders": 0.0, "revenue": 0.0}
                continue
            ins = (obj.get("insights") or {}).get("data", [])
            if not ins:
                out[cid] = {"spend": 0.0, "roas": 0.0, "orders": 0.0, "revenue": 0.0}
                continue
            row = ins[0]
            try:
                spend = float(row.get("spend", 0) or 0)
            except (TypeError, ValueError):
                spend = 0.0
            out[cid] = {
                "spend": spend,
                "roas": _extract_roas(row.get("purchase_roas")),
                "orders": _extract_action(row.get("actions")),
                "revenue": _extract_action(row.get("action_values")),
            }
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
                    "orders": 0.0, "revenue": 0.0,
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
            c["orders"] = p.get("orders", 0.0)
            c["revenue"] = p.get("revenue", 0.0)

    return by_product, yday


def fetch_creatives(cid_to_product, yday):
    """Ad-level perf for the home-decor campaigns on `yday`.

    Returns a list of {ad_id, name, product, spend, roas} for ads that spent >0,
    pulled per account (level=ad) and filtered to our home-decor campaign ids.
    """
    cids = set(cid_to_product)
    out = []
    for portal, accts in PORTAL_ACCOUNTS.items():
        for env_var, friendly in accts:
            if env_var in EXCLUDE_ACCOUNTS:
                continue
            aid = os.environ.get(env_var)
            if not aid:
                continue
            rows = meta_paginate(f"{GRAPH_API}/{aid}/insights", {
                "level": "ad",
                "fields": "ad_id,ad_name,campaign_id,spend,purchase_roas",
                "time_range": json.dumps({"since": yday, "until": yday}),
                "filtering": json.dumps([{"field": "spend",
                                          "operator": "GREATER_THAN", "value": "0"}]),
                "limit": 500,
            })
            for r in rows:
                if r.get("campaign_id") not in cids:
                    continue
                try:
                    spend = float(r.get("spend") or 0)
                except (TypeError, ValueError):
                    spend = 0.0
                out.append({
                    "ad_id": r.get("ad_id"),
                    "name": r.get("ad_name") or "(unnamed)",
                    "product": cid_to_product.get(r.get("campaign_id"), "?"),
                    "spend": spend,
                    "roas": _extract_roas(r.get("purchase_roas")),
                    "acct": aid,
                    "camp_id": r.get("campaign_id") or "",
                })
            time.sleep(0.3)
    return out


def fetch_portal_spend(yday):
    """{portal: spend, 'TOTAL': spend} — total ad spend per portal on yday (ALL
    categories, all accounts), for the store-wide ROAS in the sales block."""
    out, tot = {}, 0.0
    for portal, accts in PORTAL_ACCOUNTS.items():
        s = 0.0
        for env_var, friendly in accts:
            if env_var in EXCLUDE_ACCOUNTS:
                continue
            aid = os.environ.get(env_var)
            if not aid:
                continue
            try:
                d = meta_get(
                    f"{GRAPH_API}/{aid}/insights",
                    {"time_range": json.dumps({"since": yday, "until": yday}),
                     "fields": "spend"},
                    max_retries=3,
                )
                rows = (d or {}).get("data") or []
                if rows:
                    s += float(rows[0].get("spend") or 0)
            except Exception:  # noqa: BLE001 — fail-safe
                pass
            time.sleep(0.2)
        out[portal] = s
        tot += s
    out["TOTAL"] = tot
    return out


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


def write_sheet(by_product, yday, creatives, sales=None):
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
    # ── Store sales (Shopify, real orders — all 3 stores) ──
    values.append([])
    ss_title_row = len(values) + 1
    values.append([f"💰 STORE SALES ({yday_label}) — real Shopify orders, all 3 stores  ·  "
                   f"Gross = total paid · Net = after discounts · ROAS = Gross ÷ ad spend"])
    ss_hdr_row = len(values) + 1
    values.append(["Store", "Orders", "Gross Sales (₹)", "Net Sales (₹)", "Ad Spend (₹)", "ROAS"])
    ss_first = len(values) + 1
    if sales:
        for p in ["SM", "SML", "NBP"]:
            d = sales.get(p)
            if d is None:
                values.append([p, "—", "—", "—", "—", "—"])
            else:
                values.append([p, d["orders"], round(d["gross"]), round(d["net"]),
                               round(d.get("spend", 0)), d.get("roas", 0.0)])
        t = sales["TOTAL"]
        values.append(["TOTAL", t["orders"], round(t["gross"]), round(t["net"]),
                       round(t.get("spend", 0)), t.get("roas", 0.0)])
    else:
        values.append(["(fills with real data on the 7 AM run / GitHub Actions)",
                       "", "", "", "", ""])
    ss_last = len(values)

    values.append([])
    # Product table
    tbl_hdr = len(values) + 1
    values.append(["#", "Product", "#Camps", "Budget (₹/day)", "Spend (₹)", "ROAS", "Verdict"])
    tbl_first = len(values) + 1
    for i, (product, ncamps, budget, spend, roas) in enumerate(prod_rows, 1):
        verdict = _verdict(roas) if ncamps else "💤 Off"
        values.append([i, product, ncamps, round(budget), round(spend), roas, verdict])
    values.append(["", "── TOTAL ──", tot_camps, round(tot_budget), round(tot_spend),
                   tot_roas, ""])
    total_row = len(values)  # 1-based row of the product total

    # ── Creatives: ALL ads (spend>0), sorted by ROAS; highlight top/worst 3 ──
    ads = sorted(creatives, key=lambda c: c["roas"], reverse=True)
    qual = [c for c in ads if c["spend"] >= MIN_CREATIVE_SPEND]
    top_ids = {c["ad_id"] for c in qual[:3]}
    worst_ids = {c["ad_id"] for c in sorted(qual, key=lambda c: c["roas"])[:3]} - top_ids

    chdr = ["Rank", "Ad / Creative", "Product", "Spend (₹)", "ROAS", "Camp ID"]
    values.append([])
    cr_title_row = len(values) + 1
    values.append([f"🎬 CREATIVES PERFORMANCE — all {len(ads)} ads (spend>0), sorted by ROAS  ·  "
                   f"🏆 top 3 green · 🔻 worst 3 red (spend ≥ ₹{MIN_CREATIVE_SPEND})  ·  {yday_label}"])
    cr_hdr_row = len(values) + 1
    values.append(chdr)
    cr_first = len(values) + 1
    green_rows, red_rows = [], []
    if not ads:
        values.append(["", "(no ads with spend on this day)", "", "", "", ""])
    else:
        for i, c in enumerate(ads, 1):
            r = len(values) + 1
            acct_num = (c.get("acct") or "").replace("act_", "")
            nm = c["name"][:60]
            if acct_num and c.get("ad_id"):
                url = (f"https://business.facebook.com/adsmanager/manage/ads?"
                       f"act={acct_num}&selected_ad_ids={c['ad_id']}")
                name_cell = '=HYPERLINK("%s","%s")' % (url, nm.replace('"', '""'))
            else:
                name_cell = nm
            camp_cell = '="%s"' % c.get("camp_id", "")  # text → keeps full 18-digit id
            values.append([i, name_cell, c["product"], round(c["spend"]), c["roas"], camp_cell])
            if c["ad_id"] in top_ids:
                green_rows.append(r)
            elif c["ad_id"] in worst_ids:
                red_rows.append(r)
    cr_last = len(values)

    ws.clear()
    ws.update(range_name="A1", values=values, value_input_option="USER_ENTERED")

    # ── formatting ──
    sid = ws.id
    kpi_hdr = 4          # 1-based row of KPI header
    kpi_val = 5
    # tbl_hdr, tbl_first, ss_*, cr_*, total_row all captured during building above.
    # Toned-down palette: plain grey headings only (no bright fills). The light
    # green/red on top-3/worst-3 creative rows is kept — it's a requested signal.
    blue = {"red": 0.91, "green": 0.91, "blue": 0.91}        # header grey
    lightblue = {"red": 0.96, "green": 0.96, "blue": 0.96}   # subtle row grey
    green = {"red": 0.13, "green": 0.55, "blue": 0.13}
    lightgreen = {"red": 0.86, "green": 0.94, "blue": 0.86}
    red = {"red": 0.70, "green": 0.13, "blue": 0.13}
    lightred = {"red": 0.98, "green": 0.89, "blue": 0.89}

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
                                 "foregroundColor": {"red": 0, "green": 0, "blue": 0}}},
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
                                 "foregroundColor": {"red": 0, "green": 0, "blue": 0}}},
                 "userEnteredFormat(backgroundColor,textFormat)"),
        # Budget + Spend money cols (D,E) for table rows incl total
        cell_fmt(tbl_first - 1, total_row, 3, 5, money, "userEnteredFormat.numberFormat"),
        # ROAS col (F)
        cell_fmt(tbl_first - 1, total_row, 5, 6, roasf, "userEnteredFormat.numberFormat"),
        # Total row
        cell_fmt(total_row - 1, total_row, 0, 7,
                 {"backgroundColor": lightblue, "textFormat": {"bold": True}},
                 "userEnteredFormat(backgroundColor,textFormat)"),
        # ── Creatives section: title + header (dark blue) ──
        cell_fmt(cr_title_row - 1, cr_hdr_row, 0, 6,
                 {"backgroundColor": blue,
                  "textFormat": {"bold": True,
                                 "foregroundColor": {"red": 0, "green": 0, "blue": 0}}},
                 "userEnteredFormat(backgroundColor,textFormat)"),
        # Spend (col D) + ROAS (col E) number formats over all ad rows
        cell_fmt(cr_first - 1, cr_last, 3, 4, money, "userEnteredFormat.numberFormat"),
        cell_fmt(cr_first - 1, cr_last, 4, 5, roasf, "userEnteredFormat.numberFormat"),
    ]
    # Highlight the top-3 (green) and worst-3 (red) creative rows wherever they fall
    for r in green_rows:
        fmt.append(cell_fmt(r - 1, r, 0, 6,
                            {"backgroundColor": lightgreen, "textFormat": {"bold": True}},
                            "userEnteredFormat(backgroundColor,textFormat)"))
    for r in red_rows:
        fmt.append(cell_fmt(r - 1, r, 0, 6,
                            {"backgroundColor": lightred, "textFormat": {"bold": True}},
                            "userEnteredFormat(backgroundColor,textFormat)"))
    # Sales block: title+header (dark), money cols (Gross/Net/Ad Spend), ROAS col, bold total
    fmt.append(cell_fmt(ss_title_row - 1, ss_hdr_row, 0, 6,
                        {"backgroundColor": blue,
                         "textFormat": {"bold": True,
                                        "foregroundColor": {"red": 0, "green": 0, "blue": 0}}},
                        "userEnteredFormat(backgroundColor,textFormat)"))
    fmt.append(cell_fmt(ss_first - 1, ss_last, 2, 5, money, "userEnteredFormat.numberFormat"))
    fmt.append(cell_fmt(ss_first - 1, ss_last, 5, 6, roasf, "userEnteredFormat.numberFormat"))
    if sales:
        fmt.append(cell_fmt(ss_last - 1, ss_last, 0, 6,
                            {"backgroundColor": lightblue, "textFormat": {"bold": True}},
                            "userEnteredFormat(backgroundColor,textFormat)"))
    fmt += [
        {"updateSheetProperties": {
            "properties": {"sheetId": sid, "gridProperties": {"frozenRowCount": kpi_val}},
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
    cid_to_product = {c["cid"]: prod
                      for prod, camps in by_product.items() for c in camps}
    print(f"Fetching ad-level creatives for {len(cid_to_product)} home-decor camps...")
    creatives = fetch_creatives(cid_to_product, yday)
    print(f"  {len(creatives)} home-decor ads with spend on {yday}")
    try:
        sales = store_sales(yday)
    except Exception as e:  # noqa: BLE001 — never let sales break the report
        print(f"  Shopify sales fetch failed: {e}", file=sys.stderr)
        sales = None
    if sales:
        try:
            spend_by_portal = fetch_portal_spend(yday)
            for p in ["SM", "SML", "NBP", "TOTAL"]:
                if isinstance(sales.get(p), dict):
                    sp = spend_by_portal.get(p, 0.0)
                    sales[p]["spend"] = sp
                    sales[p]["roas"] = round(sales[p]["gross"] / sp, 2) if sp else 0.0
        except Exception as e:  # noqa: BLE001
            print(f"  portal spend fetch failed: {e}", file=sys.stderr)
    print(f"  store sales: {'ok' if sales else 'unavailable (fills on GHA)'}")
    title, n, budget, spend, roas = write_sheet(by_product, yday, creatives, sales)
    print()
    print(f"  {n} home-decor camps  |  ₹{int(budget):,}/day budget  |  "
          f"₹{int(spend):,} spend ({yday})  |  {roas}x ROAS")
    print(f"  Written to sheet {SHEET_ID} (tab {title})")


if __name__ == "__main__":
    main()
