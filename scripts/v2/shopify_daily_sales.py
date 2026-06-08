#!/usr/bin/env python3
"""
Shopify daily store sales (gross / net / #orders) per portal — fail-safe helper.

Used by the category daily reports to show the sales side next to ad spend.
Definitions:
  Gross sales = sum(total_price)    of non-cancelled orders (what customers paid).
  Net sales   = sum(subtotal_price) (after discounts, before tax / shipping).
  Orders      = count of non-cancelled orders.

store_sales() never raises — on missing creds or any API error it returns None
(or marks that portal None), so a caller's report still writes without sales.
"""
import os
import time

import requests

API_VERSION = "2024-01"

PORTAL_SHOPIFY = {
    "SM":  (os.getenv("SHOPIFY_STORE_URL"),     os.getenv("SHOPIFY_ACCESS_TOKEN")),
    "SML": (os.getenv("SHOPIFY_STORE_URL_SML"), os.getenv("SHOPIFY_ACCESS_TOKEN_SML")),
    "NBP": (os.getenv("SHOPIFY_STORE_URL_NBP"), os.getenv("SHOPIFY_ACCESS_TOKEN_NBP")),
}


def _f(v):
    try:
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return 0.0


def _fetch_orders(url, token, since_iso, until_iso):
    api = f"https://{url}/admin/api/{API_VERSION}/orders.json"
    params = {
        "created_at_min": since_iso, "created_at_max": until_iso,
        "limit": 250, "status": "any",
        "fields": "id,total_price,subtotal_price,cancelled_at",
    }
    headers = {"X-Shopify-Access-Token": token}
    out, next_url, next_params, pages = [], api, params, 0
    while next_url and pages < 50:
        pages += 1
        r = requests.get(next_url, params=next_params, headers=headers, timeout=60)
        if r.status_code == 429:
            time.sleep(int(r.headers.get("Retry-After", "5")))
            continue
        if r.status_code != 200:
            break
        out.extend(r.json().get("orders", []))
        link = r.headers.get("Link", "")
        next_url, next_params = None, None
        for part in link.split(","):
            if 'rel="next"' in part:
                next_url = part.strip().split(";")[0].strip().strip("<>")
                break
        if next_url:
            time.sleep(0.6)
    return out


def store_sales(yday):
    """yday = 'YYYY-MM-DD' (IST). Returns {portal: {orders,gross,net} | None,
    'TOTAL': {...}} or None if every portal failed / had no creds."""
    since = f"{yday}T00:00:00+05:30"
    until = f"{yday}T23:59:59+05:30"
    res, tot, any_ok = {}, {"orders": 0, "gross": 0.0, "net": 0.0}, False
    for portal, (url, token) in PORTAL_SHOPIFY.items():
        if not (url and token):
            res[portal] = None
            continue
        try:
            orders = _fetch_orders(url, token, since, until)
        except Exception:  # noqa: BLE001 — fail-safe, never break the caller
            res[portal] = None
            continue
        any_ok = True
        d = {"orders": 0, "gross": 0.0, "net": 0.0}
        for o in orders:
            if o.get("cancelled_at"):
                continue
            d["orders"] += 1
            d["gross"] += _f(o.get("total_price"))
            d["net"] += _f(o.get("subtotal_price"))
        res[portal] = d
        for k in d:
            tot[k] += d[k]
    if not any_ok:
        return None
    res["TOTAL"] = tot
    return res
