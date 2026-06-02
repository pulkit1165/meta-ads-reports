#!/usr/bin/env python3
"""SKU Units Sold report — for the daily WhatsApp push.

Reads a Shopify collection URL, extracts all SKUs in that collection,
pulls orders from the Shopify Admin API for the given window, sums units
sold per SKU, and prints a WhatsApp-friendly summary to stdout.

Used as the report body for scheduled_report.py.

Config via .env (see whatsapp-bot/.env):
  SKU_REPORT_COLLECTION_URL  Shopify storefront URL of the collection
                              default: https://studdmuffyn.com/collections/gold-jewellery
  SKU_REPORT_PORTAL          which Shopify store to query (SM/SML/NBP). default SM.
  SKU_REPORT_WINDOW          yesterday | today | 7d. default yesterday.

CLI: just `python3 sku_units_report.py` — prints to stdout.
"""
from __future__ import annotations

import os
import sys
import json
import logging
import requests
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")

# Chain in scripts' env
_extra = os.environ.get("EXTRA_ENV_FILE")
if _extra and Path(_extra).expanduser().is_file():
    load_dotenv(Path(_extra).expanduser(), override=False)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s sku :: %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("sku")

IST = ZoneInfo("Asia/Kolkata")

PORTAL_CREDS = {
    "SM":  ("SHOPIFY_STORE_URL",      "SHOPIFY_ACCESS_TOKEN"),
    "SML": ("SHOPIFY_STORE_URL_SML",  "SHOPIFY_ACCESS_TOKEN_SML"),
    "NBP": ("SHOPIFY_STORE_URL_NBP",  "SHOPIFY_ACCESS_TOKEN_NBP"),
}


def fetch_collection_skus(collection_url: str) -> dict[str, str]:
    """Return {sku: product_title} for all variants in the collection."""
    url = collection_url.rstrip("/") + "/products.json?limit=250"
    log.info("fetching collection: %s", url)
    r = requests.get(url, timeout=20)
    r.raise_for_status()
    out = {}
    for p in r.json().get("products", []):
        title = p.get("title", "?")
        for v in p.get("variants", []):
            sku = (v.get("sku") or "").strip()
            if sku:
                # If multiple variants share a SKU, keep first-seen title
                out.setdefault(sku, title)
    log.info("collection has %d unique SKUs", len(out))
    return out


def window_range(label: str) -> tuple[datetime, datetime, str]:
    """Return (since_utc, until_utc, label_pretty) for the window."""
    now_ist = datetime.now(IST)
    label = (label or "yesterday").lower()
    if label == "today":
        start = now_ist.replace(hour=0, minute=0, second=0, microsecond=0)
        end = now_ist
        pretty = f"today ({start.strftime('%d %b')} so far)"
    elif label in ("7d", "7days", "week"):
        end = now_ist
        start = end - timedelta(days=7)
        pretty = f"last 7 days"
    else:  # yesterday
        today0 = now_ist.replace(hour=0, minute=0, second=0, microsecond=0)
        start = today0 - timedelta(days=1)
        end = today0
        pretty = start.strftime("%d %b %Y")
    return start.astimezone(ZoneInfo("UTC")), end.astimezone(ZoneInfo("UTC")), pretty


def fetch_orders(store_url: str, access_token: str, since: datetime, until: datetime,
                 api_version: str = "2024-10") -> list[dict]:
    """Pull orders within window. Paginates via Link header."""
    base = store_url.rstrip("/")
    if not base.startswith("http"):
        base = f"https://{base}"
    endpoint = f"{base}/admin/api/{api_version}/orders.json"
    params = {
        "status": "any",
        "created_at_min": since.isoformat(),
        "created_at_max": until.isoformat(),
        "limit": 250,
        "fields": "id,name,created_at,line_items,cancelled_at",
    }
    headers = {"X-Shopify-Access-Token": access_token}
    orders = []
    page = 1
    next_url = None
    while True:
        log.info("fetching orders page %d", page)
        r = requests.get(next_url or endpoint, params=None if next_url else params,
                         headers=headers, timeout=60)
        r.raise_for_status()
        chunk = r.json().get("orders", [])
        orders.extend(chunk)
        # Pagination via Link header
        link = r.headers.get("Link", "")
        if 'rel="next"' not in link:
            break
        # Extract <url>; rel="next"
        next_url = None
        for part in link.split(","):
            if 'rel="next"' in part:
                next_url = part.strip().split(";")[0].strip("<> ")
                break
        if not next_url:
            break
        page += 1
        if page > 50:  # safety
            log.warning("hit pagination safety cap")
            break
    log.info("fetched %d orders", len(orders))
    return orders


def count_units(orders: list[dict], target_skus: set[str], include_cancelled: bool = False) -> tuple[dict[str, int], int]:
    """Return ({sku: units}, total_orders_with_match)."""
    units = defaultdict(int)
    orders_with_match = 0
    for o in orders:
        if not include_cancelled and o.get("cancelled_at"):
            continue
        matched = False
        for li in o.get("line_items", []):
            sku = (li.get("sku") or "").strip()
            if sku in target_skus:
                units[sku] += int(li.get("quantity", 0))
                matched = True
        if matched:
            orders_with_match += 1
    return dict(units), orders_with_match


def format_report(sku_units: dict[str, int], sku_titles: dict[str, str],
                  window_pretty: str, portal: str, orders_count: int) -> str:
    """Build a WhatsApp-friendly text summary."""
    if not sku_units:
        return (f"📊 *Gold Jewellery — Units Sold*\n"
                f"Window: {window_pretty} · Portal: {portal}\n\n"
                f"No units sold for any of the {len(sku_titles)} tracked SKUs in this window.")
    # Sort by units desc
    ordered = sorted(sku_units.items(), key=lambda kv: kv[1], reverse=True)
    total = sum(sku_units.values())
    lines = [f"📊 *Gold Jewellery — Units Sold*",
             f"Window: {window_pretty} · Portal: {portal}",
             f"{orders_count} orders · *{total} units* across {len(sku_units)} of {len(sku_titles)} SKUs",
             ""]
    for sku, qty in ordered:
        title = sku_titles.get(sku, "?")
        title_short = title if len(title) <= 50 else title[:47] + "…"
        lines.append(f"• {qty:>3} × {sku} — {title_short}")
    # Silent (zero-sold) tracked SKUs — show count only, not the full list
    zero = [s for s in sku_titles if s not in sku_units]
    if zero:
        lines.append("")
        lines.append(f"({len(zero)} other tracked SKUs sold 0 units)")
    return "\n".join(lines)


def main():
    collection_url = os.environ.get(
        "SKU_REPORT_COLLECTION_URL",
        "https://studdmuffyn.com/collections/gold-jewellery",
    )
    portal = os.environ.get("SKU_REPORT_PORTAL", "SM").upper()
    window = os.environ.get("SKU_REPORT_WINDOW", "yesterday")

    url_var, tok_var = PORTAL_CREDS.get(portal, PORTAL_CREDS["SM"])
    store_url = os.environ.get(url_var, "")
    access_token = os.environ.get(tok_var, "")
    if not store_url or not access_token:
        print(f"❌ Missing Shopify creds for portal {portal} ({url_var} / {tok_var}).", file=sys.stderr)
        sys.exit(2)

    sku_titles = fetch_collection_skus(collection_url)
    if not sku_titles:
        print("❌ No SKUs in collection — check the URL.", file=sys.stderr)
        sys.exit(2)

    since, until, pretty = window_range(window)
    orders = fetch_orders(store_url, access_token, since, until)
    sku_units, n_orders = count_units(orders, set(sku_titles.keys()))
    print(format_report(sku_units, sku_titles, pretty, portal, n_orders))


if __name__ == "__main__":
    main()
