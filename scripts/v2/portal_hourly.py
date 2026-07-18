#!/usr/bin/env python3
"""
portal_hourly.py — shared hourly roll-up helpers for the blended (Shopify ÷ Meta)
portal report.

Why blended and not per-campaign: only ~24% of Shopify orders carry a
utm_campaign, so attributing Shopify revenue to individual campaigns would drop
three quarters of real revenue. At PORTAL grain every order counts, so
    portal ROAS = all Shopify revenue that hour / all Meta spend that hour
is complete and honest. Per-campaign numbers stay on Meta pixel (camp_live.py).

Two source DBs, on two different orphan branches:
  * state/camp_snapshots.db  (branch camp-snapshots) — Meta spend, hourly
  * state/ntn.db             (branch state)          — Shopify orders, timestamped

Spend is stored CUMULATIVE-for-the-day per campaign per hour_slot, so an hourly
delta needs care: a campaign that stops appearing (paused, or dropped by the
impressions>0 filter) would make a naive SUM() fall and produce a negative
delta. We therefore carry each campaign's running max forward, which makes the
portal cumulative monotonic and every delta >= 0.
"""
from __future__ import annotations

import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

IST = timezone(timedelta(hours=5, minutes=30))

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / 'scripts'))

PORTALS = ('SM', 'SML', 'NBP')

# Friendly account names as they appear in campaign_hourly_snapshots.account_name,
# mirroring _utils.PORTAL_ACCOUNTS. Accounts outside these three portals
# (TransfersX, read-only mirrors) are deliberately excluded — they have no
# Shopify store to blend against, so including their spend would depress ROAS.
_ACCOUNT_PORTAL = {
    'SM Fragrance 01': 'SM', 'SM Skin': 'SM', 'SM Hair': 'SM',
    'SM Crystals': 'SM', 'SM Perfume': 'SM', 'SM CL 05': 'SM', 'SM CL 06': 'SM',
    'SML Skin': 'SML', 'SML Hair': 'SML', 'SML Crystals': 'SML',
    'SML CL 06': 'SML', 'SML CL 07': 'SML',
    'NBP Skin': 'NBP', 'NBP Hair/Perfume': 'NBP', 'NBP Crystals': 'NBP',
}

_EXCLUDE = re.compile(r'transfersx|read.?only', re.I)


def portal_of(account_name: str | None) -> str | None:
    """Map a Meta account name to SM / SML / NBP, or None if it isn't one of ours.

    Exact match first, then a prefix fallback for accounts added to Meta but not
    yet to PORTAL_ACCOUNTS. SML/NBP are tested before SM because 'SML ...' also
    starts with 'SM'.
    """
    if not account_name:
        return None
    name = account_name.strip()
    if name in _ACCOUNT_PORTAL:
        return _ACCOUNT_PORTAL[name]
    if _EXCLUDE.search(name):
        return None
    n = name.lower()
    if n.startswith('sml') or 'sm life' in n:
        return 'SML'
    if n.startswith('nbp') or 'nuskhe' in n or 'paras' in n:
        return 'NBP'
    if n.startswith('sm'):
        return 'SM'
    return None


# ── product classification ────────────────────────────────────────────────
try:
    from active_budget_by_product import classify_corrected as _classify
except Exception:  # pragma: no cover — fall back to the raw catalogue
    from product_catalogue import derive_product_and_category as _classify

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from classify_ads import extract_ntn_code
except Exception:  # pragma: no cover
    extract_ntn_code = lambda _t: None  # noqa: E731

_NON_PRODUCTS = {'Unmapped', 'Mix/Multiple', 'Multiple Products', ''}

_NTN_MAP: dict[str, str] = {}


def load_ntn_map(ntn_db: str) -> dict[str, str]:
    """NTN code -> product name, from product_ntn_labels.

    Without this, campaigns named by SKU code ('NTN1686_Rope_chain_...') fall
    through the keyword catalogue and get counted as no product at all, which
    under-reports the live-product count by roughly a third.
    """
    global _NTN_MAP
    try:
        con = sqlite3.connect(f'file:{ntn_db}?mode=ro', uri=True)
        try:
            _NTN_MAP = {c.upper(): p for c, p in con.execute(
                "SELECT ntn_code, product FROM product_ntn_labels "
                "WHERE product IS NOT NULL AND product <> ''") if c}
        finally:
            con.close()
    except Exception as e:
        print(f"  warn: NTN map unavailable ({e}) — product counts will be low")
        _NTN_MAP = {}
    return _NTN_MAP


def product_of(campaign_name: str) -> str | None:
    """Distinct product a campaign is selling, or None if it isn't product-specific.

    Order matters: the SKU code is the most reliable signal (it names a real
    product), so it wins over the keyword catalogue. Price-point collections
    ('299_collection_loose'), retargeting and mix campaigns legitimately return
    None — they aren't selling one identifiable product.
    """
    name = campaign_name or ''
    code = extract_ntn_code(name)
    if code and code in _NTN_MAP:
        return _NTN_MAP[code]
    try:
        product, _cat = _classify(name)
    except Exception:
        return None
    return None if product in _NON_PRODUCTS else product


# ── hour helpers ──────────────────────────────────────────────────────────
def slots_for_day(con: sqlite3.Connection, day: str) -> list[str]:
    """Every hour_slot present for `day`, ascending."""
    return [r[0] for r in con.execute(
        "SELECT DISTINCT hour_slot FROM campaign_hourly_snapshots "
        "WHERE hour_slot LIKE ? ORDER BY hour_slot", (day + '%',))]


def meta_hourly(con: sqlite3.Connection, day: str) -> dict:
    """{(portal, slot): {'cum': ₹, 'delta': ₹, 'products': n, 'campaigns': n}}.

    'cum' is monotonic (running max per campaign carried forward), so
    'delta' — this hour's actual spend — is never negative.
    'products' counts DISTINCT products live on ads in that slot: campaigns
    that are Active and spent something. That is the number the operator wants
    to watch fall as products get switched off through the day.
    """
    rows = con.execute(
        "SELECT hour_slot, account_name, campaign_id, campaign_name, "
        "       COALESCE(spend,0), COALESCE(status,'Active'), COALESCE(daily_budget,0) "
        "FROM campaign_hourly_snapshots WHERE hour_slot LIKE ? "
        "ORDER BY hour_slot", (day + '%',)).fetchall()

    slots = sorted({r[0] for r in rows})
    by_slot: dict[str, list] = {s: [] for s in slots}
    for r in rows:
        by_slot[r[0]].append(r)

    running: dict[tuple, float] = {}      # (portal, campaign_id) -> max spend so far
    prev_cum = {p: 0.0 for p in PORTALS}
    out: dict = {}

    for slot in slots:
        live_products = {p: set() for p in PORTALS}
        live_camps = {p: 0 for p in PORTALS}
        # Budget still running vs budget already switched off, as at this hour.
        # Point-in-time sums of daily_budget — NOT cumulative like spend — so a
        # campaign that gets un-paused correctly moves back to active.
        active_budget = {p: 0.0 for p in PORTALS}
        closed_budget = {p: 0.0 for p in PORTALS}
        for _s, acct, cid, cname, spend, status, budget in by_slot[slot]:
            portal = portal_of(acct)
            if portal is None:
                continue
            key = (portal, cid)
            if spend > running.get(key, 0.0):
                running[key] = spend
            if status == 'Active':
                active_budget[portal] += budget
            else:
                # Paused but it delivered today, so its budget was live earlier
                # and has since been closed out.
                closed_budget[portal] += budget
            if status == 'Active' and spend > 0:
                live_camps[portal] += 1
                prod = product_of(cname)
                if prod:
                    live_products[portal].add(prod)

        cum = {p: 0.0 for p in PORTALS}
        for (portal, _cid), v in running.items():
            cum[portal] += v

        for p in PORTALS:
            out[(p, slot)] = {
                'cum': round(cum[p], 2),
                'delta': round(max(0.0, cum[p] - prev_cum[p]), 2),
                'products': len(live_products[p]),
                'pset': live_products[p],
                'campaigns': live_camps[p],
                'active_budget': round(active_budget[p], 2),
                'closed_budget': round(closed_budget[p], 2),
            }
            prev_cum[p] = cum[p]
    return out


def shopify_hourly(con: sqlite3.Connection, day: str) -> dict:
    """{(portal, 'YYYY-MM-DD HH:00'): {'rev': ₹, 'orders': n}} for `day`.

    created_at is stored ISO with a +05:30 offset, so substr gives the IST hour
    directly. Cancelled orders are excluded — the operator wants Shopify
    reality, and a cancelled order is not revenue.
    """
    out: dict = {}
    q = ("SELECT portal, substr(created_at,1,13), "
         "       SUM(COALESCE(total_price,0)), COUNT(*) "
         "FROM shopify_orders "
         "WHERE substr(created_at,1,10) = ? AND cancelled_at IS NULL "
         "GROUP BY portal, substr(created_at,1,13)")
    for portal, hour13, rev, orders in con.execute(q, (day,)):
        if portal not in PORTALS:
            continue
        out[(portal, f"{hour13[:10]} {hour13[11:13]}:00")] = {
            'rev': round(float(rev or 0), 2), 'orders': int(orders or 0),
        }
    return out


def build_rows(snap_db: str, ntn_db: str, day: str) -> list[dict]:
    """One row per (hour, portal): portal | Shopify sale | ad spend | products.

    Hours run from the first slot with data up to the latest one, so a portal
    that spent nothing in an hour still shows a row (with its Shopify sales) —
    otherwise a quiet hour would silently vanish from the report.
    """
    load_ntn_map(ntn_db)          # must precede meta_hourly — it classifies products
    con_s = sqlite3.connect(f'file:{snap_db}?mode=ro', uri=True)
    try:
        meta = meta_hourly(con_s, day)
        slots = slots_for_day(con_s, day)
    finally:
        con_s.close()

    con_n = sqlite3.connect(f'file:{ntn_db}?mode=ro', uri=True)
    try:
        shop = shopify_hourly(con_n, day)
    finally:
        con_n.close()

    # Union of hours seen in either source, so sales before the first ad-spend
    # snapshot of the day are not dropped.
    snap_slots = set(slots)
    all_slots = sorted(snap_slots | {s for _p, s in shop})
    rows = []
    for slot in all_slots:
        # GitHub's cron skips hours under load, so an hour can have Shopify
        # sales but no Meta snapshot. Such an hour is UNKNOWN, not zero —
        # reporting 0 spend / 0 products there would read as "every product got
        # closed this hour", which is exactly the signal this report exists to
        # convey. Flag it and let consumers blank the Meta-derived columns.
        has_snap = slot in snap_slots
        for p in PORTALS:
            m = meta.get((p, slot), {})
            s = shop.get((p, slot), {})
            spend = m.get('delta', 0.0)
            rev = s.get('rev', 0.0)
            rows.append({
                'hour': slot[-5:], 'slot': slot, 'portal': p,
                'has_snap': has_snap,
                'shopify_sale': rev, 'orders': s.get('orders', 0),
                'ad_spend': spend,
                'roas': round(rev / spend, 2) if spend else 0.0,
                'products': m.get('products', 0),
                'pset': m.get('pset', set()),
                'campaigns': m.get('campaigns', 0),
                'cum_spend': m.get('cum', 0.0),
                'active_budget': m.get('active_budget', 0.0),
                'closed_budget': m.get('closed_budget', 0.0),
            })
    return rows


def all_portal_rows(rows: list[dict]) -> list[dict]:
    """One ALL row per hour.

    Products are SUMMED across portals, not deduplicated: each website carries
    its own listing, so the same product live on SM and on SML is two products
    being advertised, and closing it on one site is a real change the operator
    needs to see. (An earlier version took a distinct union, which hid exactly
    that.) Per-portal rows are unaffected — they were always per-website.
    """
    by_slot: dict[str, list[dict]] = {}
    for r in rows:
        by_slot.setdefault(r['slot'], []).append(r)
    out = []
    for slot, group in sorted(by_slot.items()):
        rev = sum(r['shopify_sale'] for r in group)
        spend = sum(r['ad_spend'] for r in group)
        # Namespace each product by its portal so the total is per-website.
        pset: set = set()
        for r in group:
            pset |= {(r['portal'], p) for p in r['pset']}
        out.append({
            'hour': slot[-5:], 'slot': slot, 'portal': 'ALL',
            'has_snap': any(r['has_snap'] for r in group),
            'shopify_sale': rev, 'orders': sum(r['orders'] for r in group),
            'ad_spend': spend,
            'roas': round(rev / spend, 2) if spend else 0.0,
            'products': len(pset), 'pset': pset,
            'campaigns': sum(r['campaigns'] for r in group),
            'cum_spend': sum(r['cum_spend'] for r in group),
            'active_budget': sum(r['active_budget'] for r in group),
            'closed_budget': sum(r['closed_budget'] for r in group),
        })
    return out


def summarise(rows: list[dict]) -> dict:
    """Day totals per portal + overall, for the header line and WhatsApp digest."""
    tot = {p: {'rev': 0.0, 'spend': 0.0, 'orders': 0, 'products': 0,
               'active_budget': 0.0, 'closed_budget': 0.0} for p in PORTALS}
    for r in rows:
        t = tot[r['portal']]
        t['rev'] += r['shopify_sale']
        t['spend'] += r['ad_spend']
        t['orders'] += r['orders']
    # Products = the count as of the LATEST hour that had any, not a sum over
    # hours — it's a live count, and summing hours would multiply it by the day.
    ordered = sorted(rows, key=lambda r: r['slot'])
    last_slot = {}
    for p in PORTALS:
        live = [r for r in ordered if r['portal'] == p and r['has_snap'] and r['products']]
        tot[p]['products'] = live[-1]['products'] if live else 0
        last_slot[p] = live[-1] if live else None
        # Budgets are a state, not a running total: report them as at the most
        # recent hour that actually has a snapshot.
        snap = [r for r in ordered if r['portal'] == p and r['has_snap']]
        if snap:
            tot[p]['active_budget'] = snap[-1]['active_budget']
            tot[p]['closed_budget'] = snap[-1]['closed_budget']
        tot[p]['roas'] = round(tot[p]['rev'] / tot[p]['spend'], 2) if tot[p]['spend'] else 0.0
    grand_rev = sum(tot[p]['rev'] for p in PORTALS)
    grand_spend = sum(tot[p]['spend'] for p in PORTALS)
    tot['ALL'] = {
        'rev': grand_rev, 'spend': grand_spend,
        'orders': sum(tot[p]['orders'] for p in PORTALS),
        # Per-website: a product live on two sites counts twice.
        'products': sum(tot[p]['products'] for p in PORTALS),
        'active_budget': sum(tot[p]['active_budget'] for p in PORTALS),
        'closed_budget': sum(tot[p]['closed_budget'] for p in PORTALS),
        'roas': round(grand_rev / grand_spend, 2) if grand_spend else 0.0,
    }
    return tot


def today_ist() -> str:
    return datetime.now(IST).strftime('%Y-%m-%d')
