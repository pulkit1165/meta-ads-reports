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
    from classify_ads import extract_ntn_code, extract_product_slug
except Exception:  # pragma: no cover
    extract_ntn_code = lambda _t: None            # noqa: E731
    extract_product_slug = lambda _t: None        # noqa: E731

# Noise tokens in a recovered slug: SKU codes, funnel/audience markers, price
# points, pack sizes, dates, creative boilerplate. What survives is the product.
_SLUG_DROP = re.compile(
    r'^(ntn\d+|st\d+[a-f]?|dup|exc|ex|copy|sm|sml|smsk|smskin|adv|web|conv|'
    r'rtg|retarget|sales?|reel|clp|mix|inde|paras|wanda|ant|creatives?|download|'
    r'call|video|dimp|master|dv|app|gateway|camp|campaign|trishul|motion|static|'
    r'partnership|ugc|installs?|boosterkit|trf|aarjav|oooh|june|loose|brand|'
    r'single|high|potential|strong|explorer|testing|ab|a|b|v\d*|new|old|test|'
    r'\d+|\d+dp|ex\d+dp|\d+d|\d+dimp|packof\d+|pack|set|combo\d+)$')

# Campaigns that are not selling one identifiable product, whatever the slug
# says: price-point collections, catalog/DPA, pure retargeting, explicit mixes.
_NOT_A_PRODUCT = re.compile(
    r'(\d+_?collection|_collection|catalog|best_?seller|imp_rtg|_rtg_\d+d|'
    r'^imp_|_mix_|multi_cat|all_category|all_products|loose_\d+_\d+)', re.I)


def _clean_slug(slug: str | None) -> str | None:
    """'~ntn1678_unstoppable_499_ex30dp' -> 'Unstoppable'.

    Note the leading '~' that extract_product_slug prepends — stripping it
    matters, because '~ntn1678' does not match the NTN drop rule and the code
    then leaks into the product name, splitting one product across every
    campaign variant that sells it.
    """
    if not slug:
        return None
    parts = [t for t in re.split(r'[_\s]+', slug.lower().lstrip('~')) if t]
    parts = [t for t in parts if not _SLUG_DROP.match(t)]
    if not parts:
        return None
    # Two tokens is enough to name a product; more just re-introduces the
    # campaign variant ('unstoppable' vs 'unstoppable ex30dp') as a fake second
    # product, which would break one-product-many-campaigns counting.
    return ' '.join(parts[:2]).title()


# Generic catalogue buckets that are categories, not identifiable products.
# Counting these as products both inflates the number and hides the real SKU,
# so they fall through to the slug tier instead.
_GENERIC = {'Jewellery', 'Offer/Combo', 'Peptide Products', 'Crystal', 'Combo'}

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

    Three tiers, most reliable first:
      1. NTN code -> product_ntn_labels. Names a real SKU.
      2. Keyword catalogue (wanda/astro-corrected).
      3. Cleaned slug from the campaign name. This is the "map it by name" tier:
         a campaign like 'ntn1678_Unstoppable_499_ex30dp_paras_single_100726'
         carries a code that is missing from the SKU sheet AND matches no
         keyword, so without this it counted as no product at all. Roughly half
         of SM's spending campaigns were being dropped that way.

    Generic catalogue buckets ('Jewellery', 'Offer/Combo') are pushed down to
    tier 3 as well — they are categories, not products, and counting them both
    inflated the total and hid the actual SKU.

    Still returns None for things genuinely not selling one product: price-point
    collections, retargeting, catalog and mix campaigns.
    """
    name = campaign_name or ''
    if _NOT_A_PRODUCT.search(name):
        return None
    code = extract_ntn_code(name)
    if code and code in _NTN_MAP:
        return _NTN_MAP[code]
    try:
        product, _cat = _classify(name)
    except Exception:
        product = None
    if product and product not in _NON_PRODUCTS and product not in _GENERIC:
        return product
    try:
        return _clean_slug(extract_product_slug(name))
    except Exception:
        return None


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


def shopify_hourly(con: sqlite3.Connection, day: str, cutoff: str | None = None) -> dict:
    """{(portal, 'YYYY-MM-DD HH:00'): {'rev': ₹, 'orders': n}} for `day`.

    created_at is stored ISO with a +05:30 offset, so substr gives the IST hour
    directly. Cancelled orders are excluded — the operator wants Shopify
    reality, and a cancelled order is not revenue.

    `cutoff` (an ISO IST timestamp) drops orders newer than the most recent Meta
    snapshot. Without it the ROAS is measured over mismatched windows: Shopify
    is ingested every 10 minutes but Meta spend only lands hourly, so revenue
    runs ahead of spend and inflates ROAS — badly when the snapshot cron skips
    a tick (4.03 against a true 0.73 in one observed case).
    """
    out: dict = {}
    q = ("SELECT portal, substr(created_at,1,13), "
         "       SUM(COALESCE(total_price,0)), COUNT(*) "
         "FROM shopify_orders "
         "WHERE substr(created_at,1,10) = ? AND cancelled_at IS NULL "
         + ("AND created_at <= ? " if cutoff else "")
         + "GROUP BY portal, substr(created_at,1,13)")
    params = (day, cutoff) if cutoff else (day,)
    for portal, hour13, rev, orders in con.execute(q, params):
        if portal not in PORTALS:
            continue
        out[(portal, f"{hour13[:10]} {hour13[11:13]}:00")] = {
            'rev': round(float(rev or 0), 2), 'orders': int(orders or 0),
        }
    return out


def latest_snapshot_ts(snap_db: str, day: str) -> str | None:
    """Wall-clock time of the newest snapshot for `day` — the moment Meta spend
    was actually measured. This is the report's true 'as of', not the build time."""
    con = sqlite3.connect(f'file:{snap_db}?mode=ro', uri=True)
    try:
        return con.execute(
            "SELECT MAX(ts) FROM campaign_hourly_snapshots WHERE hour_slot LIKE ?",
            (day + '%',)).fetchone()[0]
    finally:
        con.close()


def build_rows(snap_db: str, ntn_db: str, day: str, align: bool = True) -> list[dict]:
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

    cutoff = latest_snapshot_ts(snap_db, day) if align else None
    con_n = sqlite3.connect(f'file:{ntn_db}?mode=ro', uri=True)
    try:
        shop = shopify_hourly(con_n, day, cutoff)
    finally:
        con_n.close()

    # Union of hours seen in either source, so sales before the first ad-spend
    # snapshot of the day are not dropped. Running sales totals let each row
    # carry the DAY-TO-DATE ROAS as well as that hour's — the day-to-date
    # figure is what tells you how a website is actually tracking, since a
    # single hour swings wildly on a few orders.
    snap_slots = set(slots)
    all_slots = sorted(snap_slots | {s for _p, s in shop})
    rows = []
    run_sales = {p: 0.0 for p in PORTALS}
    run_orders = {p: 0 for p in PORTALS}
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
            run_sales[p] += rev
            run_orders[p] += s.get('orders', 0)
            cum_spend = m.get('cum', 0.0)
            rows.append({
                'cum_sales': round(run_sales[p], 2),
                'cum_orders': run_orders[p],
                'cum_roas': round(run_sales[p] / cum_spend, 2) if cum_spend else 0.0,
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
            'cum_sales': sum(r['cum_sales'] for r in group),
            'cum_orders': sum(r['cum_orders'] for r in group),
            'cum_roas': (round(sum(r['cum_sales'] for r in group)
                               / sum(r['cum_spend'] for r in group), 2)
                         if sum(r['cum_spend'] for r in group) else 0.0),
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


def closures(snap_db: str, day: str) -> list[dict]:
    """Campaigns that went Active -> Paused during `day`, newest first.

    Reconstructed from the snapshot history rather than logged as it happens,
    so it is self-healing: a closure during an hour the page never rendered
    still shows up on the next build.

    The timestamp is the FIRST snapshot that saw the campaign paused, so the
    real close happened somewhere between the previous snapshot and that one —
    within about 10 minutes. Labelled '~' for that reason.

    A campaign whose first snapshot of the day is already Paused was closed
    before we started watching; it is reported with before=True rather than
    given a false precise time.
    """
    con = sqlite3.connect(f'file:{snap_db}?mode=ro', uri=True)
    try:
        rows = con.execute(
            "SELECT campaign_id, campaign_name, account_name, hour_slot, ts, "
            "       COALESCE(status,'Active'), COALESCE(spend,0), "
            "       COALESCE(daily_budget,0), COALESCE(roas,0) "
            "FROM campaign_hourly_snapshots WHERE hour_slot LIKE ? "
            "ORDER BY campaign_id, hour_slot", (day + '%',)).fetchall()
    finally:
        con.close()

    seq: dict = {}
    for cid, name, acct, slot, ts, status, spend, budget, roas in rows:
        seq.setdefault(cid, []).append((slot, ts, status, name, acct, spend, budget, roas))

    out = []
    for cid, obs in seq.items():
        obs.sort()
        prev_status = None
        for slot, ts, status, name, acct, spend, budget, roas in obs:
            portal = portal_of(acct)
            if portal is None:
                break
            if status != 'Active' and prev_status == 'Active':
                out.append({
                    'campaign_id': cid, 'campaign_name': name or '', 'portal': portal,
                    'account_name': acct or '', 'closed_ts': ts, 'closed_slot': slot,
                    'spend': spend, 'daily_budget': budget, 'roas': roas,
                    'spend_pct': round(spend / budget * 100, 1) if budget else 0.0,
                    'before': False,
                })
                break
            if prev_status is None and status != 'Active':
                out.append({
                    'campaign_id': cid, 'campaign_name': name or '', 'portal': portal,
                    'account_name': acct or '', 'closed_ts': ts, 'closed_slot': slot,
                    'spend': spend, 'daily_budget': budget, 'roas': roas,
                    'spend_pct': round(spend / budget * 100, 1) if budget else 0.0,
                    'before': True,
                })
                break
            prev_status = status
    out.sort(key=lambda r: r['closed_ts'], reverse=True)
    return out


def slot_times(snap_db: str, day: str) -> dict:
    """{hour_slot: actual wall-clock time that slot was last measured}.

    The slot is a label, not the measurement moment: a run at 12:08 writes slot
    '12:00'. So the newest slot is nearly always a PARTIAL hour, and comparing
    it against the previous one — which was measured near :58 — makes spend look
    flat when only ten minutes have passed. Showing the real time removes that
    illusion.
    """
    con = sqlite3.connect(f'file:{snap_db}?mode=ro', uri=True)
    try:
        return {slot: ts for slot, ts in con.execute(
            "SELECT hour_slot, MAX(ts) FROM campaign_hourly_snapshots "
            "WHERE hour_slot LIKE ? GROUP BY hour_slot", (day + '%',))}
    finally:
        con.close()
