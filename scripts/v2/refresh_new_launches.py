#!/usr/bin/env python3
"""
refresh_new_launches.py — keep the homepage "New Launches" collection rolling.

The studdmuffyn.com homepage row reads the custom collection
`new-launches-home` (created 25 Jul 2026, operator request: "products that
were listed in last 10 days", newest first, all categories mixed — it used to
show only keychains). Shopify smart collections cannot rule on created_at, so
this script re-syncs the membership daily:

  * IN : active, published-to-online-store products created in the last
         WINDOW_DAYS days
  * OUT: anything older, plus hidden add-on products (the ₹99 Koi coaster
         deal must never be sold standalone — operator rule)
  * ORDER: round-robin across categories (rakhi, keychain, chain, …), each
         category newest-first. Pure recency put 7 rakhis in a row at the
         top and the operator asked for a mix (25 Jul).

Full resync (delete all collects, re-add in order) — ~25 items, simplest way
to also keep the manual sort order correct.

Env: SHOPIFY_ACCESS_TOKEN (required), SHOPIFY_STORE_URL (default studd-muffyn).
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request

WINDOW_DAYS = 10
HANDLE = 'new-launches-home'
STORE = os.environ.get('SHOPIFY_STORE_URL', 'studd-muffyn.myshopify.com')
TOKEN = os.environ.get('SHOPIFY_ACCESS_TOKEN')
API = f'https://{STORE}/admin/api/2024-10'


def req(method: str, path: str, body: dict | None = None) -> dict:
    r = urllib.request.Request(
        API + path, method=method,
        data=json.dumps(body).encode() if body else None,
        headers={'X-Shopify-Access-Token': TOKEN,
                 'Content-Type': 'application/json'})
    for attempt in range(5):
        try:
            with urllib.request.urlopen(r, timeout=30) as resp:
                return json.loads(resp.read() or b'{}')
        except urllib.error.HTTPError as e:
            if e.code == 429:            # rate limited — back off and retry
                time.sleep(2 * (attempt + 1))
                continue
            raise
    raise RuntimeError(f'{method} {path}: rate-limited after retries')


def hidden_addon(p: dict) -> bool:
    """Products that exist only as an in-page deal, never standalone."""
    t = p['title'].lower()
    return 'add-on' in t or 'addon' in (p.get('tags') or '').lower()


def category(p: dict) -> str:
    """Coarse bucket used only for interleaving the display order."""
    t = p['title'].lower()
    for key in ('rakhi', 'keychain', 'bracelet', 'pendant', 'necklace', 'chain'):
        if key in t:
            return key
    return 'other'


def interleave(prods: list[dict]) -> list[dict]:
    """Round-robin across categories, each category newest-first, so no
    single launch drop (e.g. 7 rakhis in one day) monopolises the row."""
    from collections import OrderedDict
    groups: OrderedDict[str, list[dict]] = OrderedDict()
    for p in sorted(prods, key=lambda p: p['created_at'], reverse=True):
        groups.setdefault(category(p), []).append(p)
    out = []
    while any(groups.values()):
        for g in groups.values():
            if g:
                out.append(g.pop(0))
    return out


def main() -> None:
    if not TOKEN:
        sys.exit('FATAL: SHOPIFY_ACCESS_TOKEN not set')

    cols = req('GET', f'/custom_collections.json?handle={HANDLE}')['custom_collections']
    if not cols:
        sys.exit(f'FATAL: collection {HANDLE} not found — homepage depends on it')
    cid = cols[0]['id']

    from datetime import datetime, timedelta, timezone
    since = (datetime.now(timezone(timedelta(hours=5, minutes=30)))
             - timedelta(days=WINDOW_DAYS)).strftime('%Y-%m-%dT00:00:00+05:30')
    prods = req('GET', '/products.json?limit=250&status=active'
                       f'&published_status=published&created_at_min={since}'
                       '&fields=id,title,created_at,tags')['products']
    keep = interleave([p for p in prods if not hidden_addon(p)])
    print(f'{len(keep)} products created since {since[:10]}')

    old = req('GET', f'/collects.json?collection_id={cid}&limit=250')['collects']
    for c in old:
        req('DELETE', f'/collects/{c["id"]}.json')
    for pos, p in enumerate(keep, 1):
        req('POST', '/collects.json', {'collect': {
            'collection_id': cid, 'product_id': p['id'], 'position': pos}})
        print(f'  {pos:2d}. {p["created_at"][:10]}  {p["title"][:60]}')
    print(f'resynced {HANDLE}: {len(old)} out, {len(keep)} in')


if __name__ == '__main__':
    main()
