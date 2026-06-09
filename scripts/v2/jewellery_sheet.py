#!/usr/bin/env python3
"""
Jewellery (24K Jewellery + Crystal Accessory) → Google Sheet (daily, 12 PM IST).

Same idea as home_decor_sheet.py, for jewellery / wearables. Pulls every
currently-ACTIVE campaign across all 3 portals live from Meta, keeps the
jewellery family — gold jewellery (chains / pendants / necklaces / rings) AND
crystal accessories worn on the body (bracelets, sutras, half-n-half) — i.e.
derive_category_v2 in {'24K Jewellery', 'Crystal Accessory'}, groups by product,
and writes yesterday's numbers to the operator's "jewellery" sheet:

  - KPI strip: total budget / spend (yesterday) / campaigns running / ROAS
  - PRODUCT table: gold Jewellery, Pyrite/Crystal Bracelet, sutras, etc. (the
    canonical list always shows, inactive ones as 0 / "💤 Off")
  - CREATIVES performance: every jewellery ad (spend>0) sorted by ROAS, top-3 /
    worst-3 (spend ≥ ₹500) highlighted green / red, ad name links to Ads Manager.

Window: "kal" = yesterday (IST). One new tab per day (DATA date, YYYY-MM-DD).

Run by .github/workflows/jewellery.yml on the 01:30 UTC = 07:00 IST cron. Needs
META_ACCESS_TOKEN + GOOGLE_SERVICE_ACCOUNT_FILE. The service account
antriksh-bot@antriksh-meta-reports.iam.gserviceaccount.com must have Editor
access on the sheet (operator shares it manually once).

Usage:
  python3 scripts/v2/jewellery_sheet.py
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
from product_catalogue import derive_product_and_category, derive_category_v2  # noqa: E402

# Reuse the category-agnostic helpers from the home-decor report.
import home_decor_sheet as hd  # noqa: E402

# Operator's "jewellery" sheet — service account must have Editor access.
SHEET_ID = "1MPMglzC5qhCuh85pbxHk2_PeY4kEmCtWeMvBTrH15Ko"

# Jewellery family = gold jewellery + body-worn crystal accessories.
JEWELLERY_CATEGORIES = {"24K Jewellery", "Crystal Accessory"}

# Canonical product list (always shown, inactive as 0 / "💤 Off"). "Jewellery"
# is the 24K gold bucket (chains / pendants / necklaces / rings); the rest are
# crystal accessories. Any live product not listed is appended automatically.
JEWELLERY_PRODUCTS = [
    "Jewellery", "Pyrite Bracelet", "Crystal Bracelet", "Prem Sutra Bracelet",
    "Pyrite Half & Half", "Nazar Sutra", "Sutra Range Mix",
]


# ── data collection ───────────────────────────────────────────────────────────
def collect():
    """product -> [ {cid,name,portal,acct,budget,spend,roas} ], jewellery family only."""
    if not os.getenv("META_ACCESS_TOKEN"):
        sys.exit("META_ACCESS_TOKEN not set")

    by_product = defaultdict(list)
    for portal, accts in PORTAL_ACCOUNTS.items():
        for env_var, friendly in accts:
            if env_var in hd.EXCLUDE_ACCOUNTS:
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
            n = 0
            for c in (d or {}).get("data") or []:
                name = c.get("name") or ""
                if hd.excluded(name):
                    continue
                if derive_category_v2(name) not in JEWELLERY_CATEGORIES:
                    continue
                budget = hd.per_day_budget(c)
                if budget <= 0:
                    continue
                product = derive_product_and_category(name)[0]
                by_product[product].append({
                    "cid": c["id"], "name": name, "portal": portal, "acct": aid,
                    "budget": budget, "spend": 0.0, "roas": 0.0,
                })
                n += 1
            print(f"  {portal} {friendly}: {n} jewellery active w/ budget")
            time.sleep(0.25)

    yday = (datetime.now(IST).date() - timedelta(days=1)).isoformat()
    cids = list({c["cid"] for camps in by_product.values() for c in camps})
    print(f"Fetching {yday} spend + ROAS for {len(cids)} jewellery camps...")
    perf = hd.fetch_perf_yesterday(cids, yday)
    for camps in by_product.values():
        for c in camps:
            p = perf.get(c["cid"], {})
            c["spend"] = p.get("spend", 0.0)
            c["roas"] = p.get("roas", 0.0)
    return by_product, yday


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
    title = yday
    titles = {w.title: w for w in sh.worksheets()}
    if title in titles:
        ws = titles[title]
    elif "Sheet1" in titles:
        ws = titles["Sheet1"]
        ws.update_title(title)
    else:
        ws = sh.add_worksheet(title=title, rows=120, cols=7)

    # Roll up per product; seed canonical list so inactive products still show.
    order = {p: i for i, p in enumerate(JEWELLERY_PRODUCTS)}
    products = list(JEWELLERY_PRODUCTS) + [p for p in by_product if p not in order]
    prod_rows = []
    for product in products:
        camps = by_product.get(product, [])
        budget = sum(c["budget"] for c in camps)
        spend = sum(c["spend"] for c in camps)
        rev = sum(c["spend"] * c["roas"] for c in camps)
        roas = round(rev / spend, 2) if spend else 0.0
        prod_rows.append((product, len(camps), budget, spend, roas))
    prod_rows.sort(key=lambda r: (-r[2], -r[3], order.get(r[0], 999)))

    tot_camps = sum(r[1] for r in prod_rows)
    tot_budget = sum(r[2] for r in prod_rows)
    tot_spend = sum(r[3] for r in prod_rows)
    tot_rev = sum(c["spend"] * c["roas"] for camps in by_product.values() for c in camps)
    tot_roas = round(tot_rev / tot_spend, 2) if tot_spend else 0.0

    stamp = datetime.now(IST).strftime("%d %b %y, %H:%M IST")
    yday_label = datetime.fromisoformat(yday).strftime("%d %b %Y")

    values = []
    values.append(["💍 Jewellery — Daily Report"])
    values.append([f"24K Jewellery + Crystal Accessory (gold chains / pendants, bracelets, "
                   f"sutras). Data: {yday_label} (yesterday). Refreshed {stamp}"])
    values.append([])
    values.append(["Total Budget (₹/day)", "Spend (yesterday ₹)", "Campaigns Running", "ROAS"])
    values.append([round(tot_budget), round(tot_spend), tot_camps, tot_roas])
    values.append([])
    # ── Store sales from Meta (ad-attributed, all 3 stores, all categories) ──
    ss_title_row = len(values) + 1
    values.append([f"💰 STORE SALES ({yday_label}) — Meta ad-attributed (7d click), all 3 stores  ·  "
                   f"Gross = purchase value · ROAS = Gross ÷ ad spend"])
    ss_hdr_row = len(values) + 1
    values.append(["Store", "Orders", "Gross Sales (₹)", "Ad Spend (₹)", "ROAS"])
    ss_first = len(values) + 1
    if sales:
        for p in ["SM", "SML", "NBP"]:
            d = sales.get(p) or {"orders": 0, "gross": 0, "spend": 0}
            rr = round(d["gross"] / d["spend"], 2) if d["spend"] else 0.0
            values.append([p, round(d["orders"]), round(d["gross"]), round(d["spend"]), rr])
        t = sales["TOTAL"]
        rr = round(t["gross"] / t["spend"], 2) if t["spend"] else 0.0
        values.append(["TOTAL", round(t["orders"]), round(t["gross"]), round(t["spend"]), rr])
    else:
        values.append(["(sales unavailable)", "", "", "", ""])
    ss_last = len(values)

    values.append([])
    tbl_hdr = len(values) + 1
    values.append(["#", "Product", "#Camps", "Budget (₹/day)", "Spend (₹)", "ROAS", "Verdict"])
    tbl_first = len(values) + 1
    for i, (product, ncamps, budget, spend, roas) in enumerate(prod_rows, 1):
        verdict = hd._verdict(roas) if ncamps else "💤 Off"
        values.append([i, product, ncamps, round(budget), round(spend), roas, verdict])
    values.append(["", "── TOTAL ──", tot_camps, round(tot_budget), round(tot_spend),
                   tot_roas, ""])
    total_row = len(values)

    # ── Creatives: ALL ads (spend>0), sorted by ROAS; highlight top/worst 3 ──
    ads = sorted(creatives, key=lambda c: c["roas"], reverse=True)
    qual = [c for c in ads if c["spend"] >= hd.MIN_CREATIVE_SPEND]
    top_ids = {c["ad_id"] for c in qual[:3]}
    worst_ids = {c["ad_id"] for c in sorted(qual, key=lambda c: c["roas"])[:3]} - top_ids

    chdr = ["Rank", "Ad / Creative", "Product", "Spend (₹)", "ROAS", "Camp ID"]
    values.append([])
    cr_title_row = len(values) + 1
    values.append([f"🎬 CREATIVES PERFORMANCE — all {len(ads)} ads (spend>0), sorted by ROAS  ·  "
                   f"🏆 top 3 green · 🔻 worst 3 red (spend ≥ ₹{hd.MIN_CREATIVE_SPEND})  ·  {yday_label}"])
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

    # ── Paras creatives (separated, at the bottom) ──
    paras = [c for c in ads if "paras" in (c["name"] or "").lower()]
    values.append([])
    pr_title_row = len(values) + 1
    values.append([f"🙋 PARAS CREATIVES ({len(paras)} ads) — {yday_label}, sorted by ROAS"])
    pr_hdr_row = len(values) + 1
    values.append(chdr)
    pr_first = len(values) + 1
    if not paras:
        values.append(["", "(no Paras ads with spend)", "", "", "", ""])
    else:
        for i, c in enumerate(paras, 1):
            acct_num = (c.get("acct") or "").replace("act_", "")
            nm = c["name"][:60]
            if acct_num and c.get("ad_id"):
                url = (f"https://business.facebook.com/adsmanager/manage/ads?"
                       f"act={acct_num}&selected_ad_ids={c['ad_id']}")
                name_cell = '=HYPERLINK("%s","%s")' % (url, nm.replace('"', '""'))
            else:
                name_cell = nm
            camp_cell = '="%s"' % c.get("camp_id", "")
            values.append([i, name_cell, c["product"], round(c["spend"]), c["roas"], camp_cell])
    pr_last = len(values)

    ws.clear()
    ws.update(range_name="A1", values=values, value_input_option="USER_ENTERED")

    # ── formatting ──
    sid = ws.id
    kpi_hdr, kpi_val = 4, 5
    # tbl_hdr, tbl_first, ss_*, cr_*, total_row captured during building above.
    # Toned-down palette: plain grey headings only (kept green/red on top/worst).
    purple = {"red": 0.91, "green": 0.91, "blue": 0.91}        # header grey
    lightpurple = {"red": 0.96, "green": 0.96, "blue": 0.96}   # subtle row grey
    lightgreen = {"red": 0.86, "green": 0.94, "blue": 0.86}
    lightred = {"red": 0.98, "green": 0.89, "blue": 0.89}
    white = {"red": 0, "green": 0, "blue": 0}                   # header text (black)

    def cell_fmt(r0, r1, c0, c1, body, fields):
        return {"repeatCell": {
            "range": {"sheetId": sid, "startRowIndex": r0, "endRowIndex": r1,
                      "startColumnIndex": c0, "endColumnIndex": c1},
            "cell": {"userEnteredFormat": body}, "fields": fields}}

    money = {"numberFormat": {"type": "NUMBER", "pattern": "\"₹\"#,##0"}}
    roasf = {"numberFormat": {"type": "NUMBER", "pattern": "0.00\"x\""}}

    fmt = [
        cell_fmt(0, 1, 0, 7,
                 {"backgroundColor": purple,
                  "textFormat": {"bold": True, "fontSize": 13, "foregroundColor": white}},
                 "userEnteredFormat(backgroundColor,textFormat)"),
        cell_fmt(kpi_hdr - 1, kpi_hdr, 0, 4,
                 {"backgroundColor": lightpurple, "textFormat": {"bold": True},
                  "horizontalAlignment": "CENTER"},
                 "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment)"),
        cell_fmt(kpi_val - 1, kpi_val, 0, 4,
                 {"textFormat": {"bold": True, "fontSize": 12}, "horizontalAlignment": "CENTER"},
                 "userEnteredFormat(textFormat,horizontalAlignment)"),
        cell_fmt(kpi_val - 1, kpi_val, 0, 2, money, "userEnteredFormat.numberFormat"),
        cell_fmt(kpi_val - 1, kpi_val, 3, 4, roasf, "userEnteredFormat.numberFormat"),
        cell_fmt(tbl_hdr - 1, tbl_hdr, 0, 7,
                 {"backgroundColor": purple, "textFormat": {"bold": True, "foregroundColor": white}},
                 "userEnteredFormat(backgroundColor,textFormat)"),
        cell_fmt(tbl_first - 1, total_row, 3, 5, money, "userEnteredFormat.numberFormat"),
        cell_fmt(tbl_first - 1, total_row, 5, 6, roasf, "userEnteredFormat.numberFormat"),
        cell_fmt(total_row - 1, total_row, 0, 7,
                 {"backgroundColor": lightpurple, "textFormat": {"bold": True}},
                 "userEnteredFormat(backgroundColor,textFormat)"),
        # Store-sales block (5 cols): title+header grey, money cols, ROAS, bold total
        cell_fmt(ss_title_row - 1, ss_hdr_row, 0, 5,
                 {"backgroundColor": purple, "textFormat": {"bold": True, "foregroundColor": white}},
                 "userEnteredFormat(backgroundColor,textFormat)"),
        cell_fmt(ss_first - 1, ss_last, 2, 4, money, "userEnteredFormat.numberFormat"),
        cell_fmt(ss_first - 1, ss_last, 4, 5, roasf, "userEnteredFormat.numberFormat"),
        cell_fmt(ss_last - 1, ss_last, 0, 5,
                 {"backgroundColor": lightpurple, "textFormat": {"bold": True}},
                 "userEnteredFormat(backgroundColor,textFormat)"),
        cell_fmt(cr_title_row - 1, cr_hdr_row, 0, 6,
                 {"backgroundColor": purple, "textFormat": {"bold": True, "foregroundColor": white}},
                 "userEnteredFormat(backgroundColor,textFormat)"),
        cell_fmt(cr_first - 1, cr_last, 3, 4, money, "userEnteredFormat.numberFormat"),
        cell_fmt(cr_first - 1, cr_last, 4, 5, roasf, "userEnteredFormat.numberFormat"),
    ]
    for r in green_rows:
        fmt.append(cell_fmt(r - 1, r, 0, 6,
                            {"backgroundColor": lightgreen, "textFormat": {"bold": True}},
                            "userEnteredFormat(backgroundColor,textFormat)"))
    for r in red_rows:
        fmt.append(cell_fmt(r - 1, r, 0, 6,
                            {"backgroundColor": lightred, "textFormat": {"bold": True}},
                            "userEnteredFormat(backgroundColor,textFormat)"))
    # Paras section: title+header grey, money + ROAS number formats
    fmt.append(cell_fmt(pr_title_row - 1, pr_hdr_row, 0, 6,
                        {"backgroundColor": purple, "textFormat": {"bold": True, "foregroundColor": white}},
                        "userEnteredFormat(backgroundColor,textFormat)"))
    fmt.append(cell_fmt(pr_first - 1, pr_last, 3, 4, money, "userEnteredFormat.numberFormat"))
    fmt.append(cell_fmt(pr_first - 1, pr_last, 4, 5, roasf, "userEnteredFormat.numberFormat"))
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
    print(f"Jewellery → Sheet — {datetime.now(IST).strftime('%d %b %y %H:%M IST')}")
    by_product, yday = collect()
    if not by_product:
        sys.exit("No active jewellery campaigns with budget found — not writing sheet.")
    cid_to_product = {c["cid"]: prod
                      for prod, camps in by_product.items() for c in camps}
    print(f"Fetching ad-level creatives for {len(cid_to_product)} jewellery camps...")
    creatives = hd.fetch_creatives(cid_to_product, yday)
    print(f"  {len(creatives)} jewellery ads with spend on {yday}")
    try:
        sales = hd.fetch_portal_sales(yday)
    except Exception as e:  # noqa: BLE001
        print(f"  portal sales fetch failed: {e}", file=sys.stderr)
        sales = None
    print(f"  store sales: {'ok' if sales else 'unavailable'}")
    title, n, budget, spend, roas = write_sheet(by_product, yday, creatives, sales)
    print()
    print(f"  {n} jewellery camps  |  ₹{int(budget):,}/day budget  |  "
          f"₹{int(spend):,} spend ({yday})  |  {roas}x ROAS")
    print(f"  Written to sheet {SHEET_ID} (tab {title})")


if __name__ == "__main__":
    main()
