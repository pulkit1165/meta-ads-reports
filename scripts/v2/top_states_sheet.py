#!/usr/bin/env python3
"""
Top-States Performance → Google Sheet (daily, 12 PM IST).

Pulls MONTH-TO-DATE Shopify orders (1st of month → today) from all 3 stores
(NBP / SML / SM), counts unique orders per shipping state, and writes the TOP 10
states with per-portal columns (NBP / SML / SM) + merged Total + Share to a new
`States YYYY-MM-DD` tab in the operator's sheet (18shZsLz…, the same sheet as
the top-products and age-group reports).

Runs by .github/workflows/top-states.yml on a daily cron. Needs the Shopify
secrets (SHOPIFY_STORE_URL[_SML/_NBP] + SHOPIFY_ACCESS_TOKEN[_SML/_NBP]) and
GOOGLE_SERVICE_ACCOUNT_FILE. Service account antriksh-bot@… must have Editor on
the sheet. Locally it also falls back to the repo-root .env's _1/_2/_3 names.

Usage:
  python3 scripts/v2/top_states_sheet.py
"""
import os
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import gspread
import requests
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _utils import IST  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent.parent          # meta-ads-reports/
load_dotenv(REPO_ROOT / ".env")
load_dotenv(REPO_ROOT.parent / ".env")                              # C:\…\claude\.env

SHEET_ID    = "18shZsLzcI6NEUfJFUH3ZNUm6Za8a7aNegea0T2VyZSc"
API_VERSION = os.getenv("API_VERSION") or "2024-10"
TOP_N       = 10

STORE_ENVS = [
    ("NBP", ["SHOPIFY_STORE_URL_NBP", "SHOPIFY_STORE_URL_1"],
            ["SHOPIFY_ACCESS_TOKEN_NBP", "SHOPIFY_ACCESS_TOKEN_1"]),
    ("SML", ["SHOPIFY_STORE_URL_SML", "SHOPIFY_STORE_URL_2"],
            ["SHOPIFY_ACCESS_TOKEN_SML", "SHOPIFY_ACCESS_TOKEN_2"]),
    ("SM",  ["SHOPIFY_STORE_URL", "SHOPIFY_STORE_URL_3"],
            ["SHOPIFY_ACCESS_TOKEN", "SHOPIFY_ACCESS_TOKEN_3"]),
]


def _first_env(names):
    for n in names:
        v = os.getenv(n)
        if v:
            return v
    return None


def resolve_stores():
    stores = []
    for portal, url_names, tok_names in STORE_ENVS:
        url, tok = _first_env(url_names), _first_env(tok_names)
        if url and tok:
            stores.append((portal, url, tok))
        else:
            print(f"  ⚠️  {portal}: missing url/token, skipping", file=sys.stderr)
    return stores


def _shop_host(store_url):
    """Normalize to `<sub>.myshopify.com` whether the env value is bare or a
    full domain (with/without scheme or trailing slash)."""
    s = store_url.strip().replace("https://", "").replace("http://", "").strip("/")
    if s.endswith(".myshopify.com"):
        s = s[: -len(".myshopify.com")]
    return f"{s}.myshopify.com"


def fetch_orders_by_state(store_url, token, from_date, to_date):
    """{state: set(order_id)} for the date range (by shipping address)."""
    endpoint = f"https://{_shop_host(store_url)}/admin/api/{API_VERSION}/graphql.json"
    headers = {"X-Shopify-Access-Token": token, "Content-Type": "application/json"}
    by_state = defaultdict(set)
    cursor = None

    while True:
        after = f', after: "{cursor}"' if cursor else ""
        filt  = f"created_at:>='{from_date}T00:00:00' created_at:<='{to_date}T23:59:59'"
        query = f"""{{
          orders(first: 100, query: "{filt}"{after}) {{
            edges {{ cursor node {{
              id
              shippingAddress {{ province }}
              billingAddress {{ province }}
            }} }}
            pageInfo {{ hasNextPage endCursor }}
          }}
        }}"""

        data = None
        for attempt in range(5):
            r = requests.post(endpoint, json={"query": query}, headers=headers, timeout=60)
            if r.status_code == 200:
                data = r.json()
                break
            print(f"  HTTP {r.status_code} (attempt {attempt+1}/5), retrying...", file=sys.stderr)
            time.sleep(2 * (attempt + 1))
        if data is None:
            print("  Giving up on this page after 5 attempts", file=sys.stderr)
            break
        if "errors" in data:
            print(f"  GraphQL errors: {data['errors']}", file=sys.stderr)
            break

        orders = data["data"]["orders"]
        for edge in orders["edges"]:
            node = edge["node"]
            ship = node.get("shippingAddress") or {}
            bill = node.get("billingAddress") or {}
            state = ship.get("province") or bill.get("province") or "Unknown"
            by_state[state].add(node["id"])
        if not orders["pageInfo"]["hasNextPage"]:
            break
        cursor = orders["pageInfo"]["endCursor"]
        time.sleep(0.2)

    return by_state


def collect(stores, from_date, to_date):
    """state -> {portal: order_count}."""
    per_portal = defaultdict(dict)
    for portal, url, token in stores:
        print(f"  {portal}...")
        by_state = fetch_orders_by_state(url, token, from_date, to_date)
        for state, ids in by_state.items():
            per_portal[state][portal] = len(ids)
    return per_portal


# ── sheet writing ─────────────────────────────────────────────────────────────
def open_sheet():
    sa = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE")
    if not sa or not os.path.isfile(sa):
        sys.exit(f"GOOGLE_SERVICE_ACCOUNT_FILE missing or invalid: {sa}")
    scopes = ["https://www.googleapis.com/auth/spreadsheets",
              "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_file(sa, scopes=scopes)
    return gspread.authorize(creds).open_by_key(SHEET_ID)


def write_sheet(per_portal, since, until, portals):
    sh = open_sheet()
    title = "States " + datetime.now(IST).strftime("%Y-%m-%d")
    titles = {w.title: w for w in sh.worksheets()}
    ws = titles.get(title) or sh.add_worksheet(title=title, rows=30, cols=7)

    def pc(state, p):
        return per_portal.get(state, {}).get(p, 0)

    totals = {st: sum(d.values()) for st, d in per_portal.items()}
    grand = sum(totals.values())
    ranked = sorted(totals, key=lambda k: totals[k], reverse=True)

    headers = ["Rank", "State"] + portals + ["Total", "Share %"]
    n = len(headers)
    stamp = datetime.now(IST).strftime("%d %b %y, %H:%M IST")

    values = [["📍 Top States by Orders — Shopify"]]
    values.append([f"Month-to-date {since} → {until}, all portals (NBP/SML/SM). "
                   f"Refreshed {stamp}"])
    values.append([])
    values.append(headers)
    for i, st in enumerate(ranked[:TOP_N], 1):
        share = totals[st] / grand if grand else 0
        values.append([i, st] + [pc(st, p) for p in portals] + [totals[st], share])
    portal_totals = [sum(pc(st, p) for st in ranked) for p in portals]
    values.append(["", "TOTAL (all states)"] + portal_totals + [grand, 1.0 if grand else 0])

    ws.clear()
    ws.update(range_name="A1", values=values, value_input_option="USER_ENTERED")

    sid = ws.id
    last = len(values)
    money_start, money_end = 2, 2 + len(portals) + 1
    fmt = [
        {"repeatCell": {  # banner
            "range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": 1,
                      "startColumnIndex": 0, "endColumnIndex": n},
            "cell": {"userEnteredFormat": {
                "backgroundColor": {"red": 0.12, "green": 0.31, "blue": 0.47},
                "textFormat": {"bold": True, "fontSize": 13,
                               "foregroundColor": {"red": 1, "green": 1, "blue": 1}}}},
            "fields": "userEnteredFormat(backgroundColor,textFormat)"}},
        {"repeatCell": {  # header row
            "range": {"sheetId": sid, "startRowIndex": 3, "endRowIndex": 4,
                      "startColumnIndex": 0, "endColumnIndex": n},
            "cell": {"userEnteredFormat": {
                "backgroundColor": {"red": 1.0, "green": 0.95, "blue": 0.80},
                "textFormat": {"bold": True}}},
            "fields": "userEnteredFormat(backgroundColor,textFormat)"}},
        {"repeatCell": {  # integer count cols (portals + Total)
            "range": {"sheetId": sid, "startRowIndex": 4, "endRowIndex": last,
                      "startColumnIndex": money_start, "endColumnIndex": money_end},
            "cell": {"userEnteredFormat": {"numberFormat": {
                "type": "NUMBER", "pattern": "#,##0"}}},
            "fields": "userEnteredFormat.numberFormat"}},
        {"repeatCell": {  # share %
            "range": {"sheetId": sid, "startRowIndex": 4, "endRowIndex": last,
                      "startColumnIndex": n - 1, "endColumnIndex": n},
            "cell": {"userEnteredFormat": {"numberFormat": {
                "type": "PERCENT", "pattern": "0.0%"}}},
            "fields": "userEnteredFormat.numberFormat"}},
        {"repeatCell": {  # TOTAL row
            "range": {"sheetId": sid, "startRowIndex": last - 1, "endRowIndex": last,
                      "startColumnIndex": 0, "endColumnIndex": n},
            "cell": {"userEnteredFormat": {
                "backgroundColor": {"red": 0.91, "green": 0.91, "blue": 0.91},
                "textFormat": {"bold": True}}},
            "fields": "userEnteredFormat(backgroundColor,textFormat)"}},
        {"updateSheetProperties": {
            "properties": {"sheetId": sid, "gridProperties": {"frozenRowCount": 4}},
            "fields": "gridProperties.frozenRowCount"}},
        {"autoResizeDimensions": {
            "dimensions": {"sheetId": sid, "dimension": "COLUMNS",
                           "startIndex": 0, "endIndex": n}}},
    ]
    sh.batch_update({"requests": fmt})
    return grand, title


def main():
    now = datetime.now(IST)
    since = now.replace(day=1).strftime("%Y-%m-%d")
    until = now.strftime("%Y-%m-%d")
    print(f"Top-States Performance -> Sheet - month-to-date {since} to {until} "
          f"({now.strftime('%d %b %y %H:%M IST')})")
    stores = resolve_stores()
    if not stores:
        sys.exit("No Shopify stores resolved (missing creds) — aborting.")
    portals = [s[0] for s in stores]
    per_portal = collect(stores, since, until)
    if not per_portal:
        sys.exit("No states found — not writing sheet.")
    grand, title = write_sheet(per_portal, since, until, portals)
    print()
    print(f"  {len(per_portal)} states  |  {grand:,} orders  |  top {TOP_N} written")
    print(f"  Written to sheet {SHEET_ID} (tab {title})")


if __name__ == "__main__":
    main()
