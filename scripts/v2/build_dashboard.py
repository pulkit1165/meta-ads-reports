#!/usr/bin/env python3
"""
NTN Dashboard v2 — analytical dashboard generator (rewrite).

Reads from state/ntn.db, dumps the full ad-day grid + lookups as JSON, and
renders a single-page HTML where filters/sorting/aggregation all happen
client-side. Once loaded, the dashboard re-renders every filter combination
in <100ms with no API calls and no re-fetch.

Architecture:
  - DB → JSON payload (one row per ad-day, plus lookups)
  - HTML with embedded data + Chart.js
  - JS filters/aggregates on every interaction

Usage:
  python3 scripts/v2/build_dashboard.py
  python3 scripts/v2/build_dashboard.py --days 60
  python3 scripts/v2/build_dashboard.py --out custom.html
"""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _utils import (  # noqa: E402
    db_connect, IST, now_iso, meta_get, GRAPH_API, MetaRateLimitError,
)

# product_catalogue lives in scripts/ (parent of this v2/ dir) — used to
# classify tonight's new campaigns by name on the Heatmap page card.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from product_catalogue import (  # noqa: E402
    derive_category_v2, derive_product_and_category,
)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
OUT_DIR = REPO_ROOT / 'out'
OUT_DIR.mkdir(parents=True, exist_ok=True)


def fetch_ad_days(conn, since: str, until: str):
    """One row per (ad_id, date) with everything the dashboard needs.
    JOINs meta_ads_meta for classifications + lifetime stats.
    Filtered to ads with spend>0 in the period to keep payload small."""
    rows = conn.execute('''
        SELECT
          d.date,
          d.ad_id,
          d.portal,
          d.account_name,
          d.campaign_id,
          d.campaign_name,
          d.adset_id,
          d.adset_name,
          d.ad_name,
          d.spend,
          d.impressions,
          d.reach,
          d.clicks,
          d.inline_link_clicks,
          d.outbound_clicks,
          d.ctr,
          d.cpm,
          d.cpc,
          d.frequency,
          d.purchases,
          d.revenue,
          d.add_to_cart,
          d.landing_page_views,
          d.video_thruplay,
          m.category,
          m.creative_type,
          COALESCE(sl.label, m.sentiment) AS sentiment,
          m.sentiment AS sentiment_code,
          COALESCE(m.product, p.product, m.ntn_code, '(no product tag)') AS product,
          m.ntn_code,
          m.first_seen,
          m.last_seen,
          m.days_active,
          m.total_spend AS lifetime_spend,
          m.total_revenue AS lifetime_revenue,
          m.total_purchases AS lifetime_purchases,
          m.effective_status
        FROM meta_ads_daily d
        LEFT JOIN meta_ads_meta  m ON d.ad_id = m.ad_id
        LEFT JOIN sentiment_labels sl ON sl.code = m.sentiment
        LEFT JOIN product_ntn_labels p ON p.ntn_code = m.ntn_code
        WHERE d.date BETWEEN ? AND ? AND d.spend > 0
        ORDER BY d.date, d.ad_id
    ''', (since, until)).fetchall()
    cols = ['date', 'ad_id', 'portal', 'account_name', 'campaign_id',
            'campaign_name', 'adset_id', 'adset_name', 'ad_name',
            'spend', 'impressions', 'reach', 'clicks', 'inline_link_clicks',
            'outbound_clicks', 'ctr', 'cpm', 'cpc', 'frequency',
            'purchases', 'revenue', 'add_to_cart', 'landing_page_views',
            'video_thruplay',
            'category', 'creative_type', 'sentiment', 'sentiment_code',
            'product', 'ntn_code', 'first_seen', 'last_seen', 'days_active',
            'lifetime_spend', 'lifetime_revenue', 'lifetime_purchases',
            'effective_status']
    out = []
    for r in rows:
        d = dict(zip(cols, r))
        # Round/clean numerics for smaller payload
        for k in ('spend', 'revenue', 'cpm', 'cpc', 'frequency',
                  'lifetime_spend', 'lifetime_revenue'):
            if d.get(k) is not None:
                d[k] = round(float(d[k]), 2) if d[k] else 0
        for k in ('ctr',):
            if d.get(k) is not None:
                d[k] = round(float(d[k]), 3) if d[k] else 0
        # Strip null fields to shrink payload
        d = {k: v for k, v in d.items() if v is not None}
        out.append(d)
    return out


def fetch_dimensions(conn, since: str, until: str):
    """Pre-compute distinct filter values + their counts so dropdowns are
    quick to populate without scanning the full payload."""
    def distinct(col):
        # Note: literal column substitution is safe — values are hard-coded
        return [r[0] for r in conn.execute(
            f'''SELECT DISTINCT {col} FROM meta_ads_daily d
                LEFT JOIN meta_ads_meta m ON d.ad_id = m.ad_id
                WHERE d.date BETWEEN ? AND ? AND {col} IS NOT NULL
                ORDER BY {col}''',
            (since, until)
        ).fetchall()]
    return {
        'portals':        distinct('d.portal'),
        'categories':     distinct('m.category'),
        'creative_types': distinct('m.creative_type'),
        'sentiments':     distinct('m.sentiment'),
        'products':       distinct(
            "COALESCE(m.product, m.ntn_code, '(no product tag)')"
        ),
        'accounts':       distinct('d.account_name'),
    }


def fetch_active_campaign_budgets(conn, since: str, until: str):
    """Return campaign_id -> {daily_budget, name, status, portal} for all
    campaigns currently ACTIVE that had ads with spend in the period.

    Used by the Categories page Budget Allocation chart so the pie reflects
    configured spend ceilings, not just what was already spent.
    """
    rows = conn.execute('''
        SELECT c.campaign_id, c.portal, c.name, c.effective_status,
               COALESCE(c.daily_budget, 0) AS daily,
               COALESCE(c.lifetime_budget, 0) AS lifetime
        FROM meta_campaigns c
        WHERE c.campaign_id IN (
            SELECT DISTINCT campaign_id FROM meta_ads_daily
            WHERE date BETWEEN ? AND ? AND campaign_id IS NOT NULL
        )
    ''', (since, until)).fetchall()
    out = {}
    for r in rows:
        out[r[0]] = {
            'portal': r[1],
            'name': r[2] or '',
            'status': r[3] or '',
            'daily_budget': float(r[4] or 0),
            'lifetime_budget': float(r[5] or 0),
        }
    return out


# How many days of newly-started campaigns the "Aaj ki Nayi Ads" card keeps
# browsable in its date dropdown.
DAYS_NEW_ADS = 7


def _accounts_from_env_file():
    """[(portal, act_id)] from config/accounts.env (committed, not secret)."""
    path = REPO_ROOT / 'config' / 'accounts.env'
    out = []
    if not path.is_file():
        return out
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        label, aid = (x.strip() for x in line.split('=', 1))
        portal = ('SML' if label.startswith('SML_') else
                  'SM'  if label.startswith('SM_')  else
                  'NBP' if label.startswith('NBP_') else None)
        if portal and aid.startswith('act_'):
            out.append((portal, aid))
    return out


def _live_active_campaigns():
    """ALL currently-ACTIVE campaigns straight from Meta — matches Ads Manager.

    The DB's effective_status goes stale (paused/ended campaigns linger as
    ACTIVE, and duplicates pile up), which over-counted the budget. The live
    list fixes that. No date filter here — the caller picks the most-recent
    start date (so the card always shows the latest batch, never blank between
    midnight and tonight's push). Returns rows shaped like the DB query:
    (cid, portal, name, daily, lifetime, start_time, stop_time), budgets in ₹,
    or None if there's no token / nothing came back (caller falls back to DB).
    """
    import time as _t
    if not os.getenv('META_ACCESS_TOKEN'):
        return None
    accts = _accounts_from_env_file()
    if not accts:
        return None
    rows, got_any = [], False
    for portal, aid in accts:
        try:
            d = meta_get(
                f"{GRAPH_API}/{aid}/campaigns",
                {'fields': 'id,name,daily_budget,lifetime_budget,'
                           'start_time,stop_time,'
                           'adsets.limit(50){effective_status,daily_budget,'
                           'lifetime_budget}',
                 'effective_status': '["ACTIVE"]', 'limit': 500},
                max_retries=2,
            )
        except (MetaRateLimitError, Exception):  # noqa: BLE001
            continue
        data = (d or {}).get('data')
        if data is None:
            continue                                  # error for this account
        got_any = True
        for c in data:
            db = float(c.get('daily_budget') or 0) / 100
            lb = float(c.get('lifetime_budget') or 0) / 100
            # CBO off → budget lives on the ad sets. Sum the ACTIVE ad-sets'
            # budgets so "pushed" isn't ₹0 (which made spend look > pushed).
            if db == 0 and lb == 0:
                adsets = ((c.get('adsets') or {}).get('data')) or []
                db = sum(float(a.get('daily_budget') or 0) / 100
                         for a in adsets if a.get('effective_status') == 'ACTIVE')
                if db == 0:
                    lb = sum(float(a.get('lifetime_budget') or 0) / 100
                             for a in adsets if a.get('effective_status') == 'ACTIVE')
            rows.append((
                c.get('id'), portal, c.get('name') or '',
                db, lb, c.get('start_time'), c.get('stop_time'),
            ))
        _t.sleep(0.2)
    return rows if got_any else None


def _schedule_days(start_time, stop_time):
    """Whole days between a campaign's start and stop ISO timestamps (min 1),
    or 0 if either is missing/unparseable. Used to turn a lifetime budget into
    an effective per-day figure."""
    if not start_time or not stop_time:
        return 0
    try:
        a = datetime.fromisoformat(start_time)
        b = datetime.fromisoformat(stop_time)
    except (ValueError, TypeError):
        return 0
    days = (b - a).days
    return days if days >= 1 else 1


def fetch_new_today(conn):
    """Campaigns that STARTED today (IST) and are currently ACTIVE — i.e. the
    ads pushed tonight. Classified by name (v2 category + product) and carrying
    their configured daily budget. Powers the Heatmap page's 'Aaj ki Nayi Ads'
    card.

    Recomputed on every hourly rebuild and keyed off start_time's date, so it
    is a rolling "today" that resets at IST midnight. created_time isn't stored
    in meta_ads_meta (always NULL here), so start_time is the reliable
    "went live today" signal — it matches the live created_time count within a
    camp or two.
    """
    today = datetime.now(IST).strftime('%Y-%m-%d')
    window_start = (datetime.now(IST).date() - timedelta(days=DAYS_NEW_ADS)).isoformat()

    # Prefer the LIVE Meta list (matches Ads Manager — the DB's ACTIVE status
    # goes stale and over-counts). Fall back to the DB if there's no token.
    all_rows = _live_active_campaigns()
    if all_rows is None:
        all_rows = conn.execute('''
            SELECT campaign_id, portal, name,
                   COALESCE(daily_budget, 0)    AS daily,
                   COALESCE(lifetime_budget, 0) AS lifetime,
                   start_time, stop_time
            FROM meta_campaigns
            WHERE effective_status = 'ACTIVE'
        ''').fetchall()

    # Actual spend per (campaign, date) — each ad shows the spend on the date
    # being viewed, so it's comparable to the per-DAY pushed budget (Meta caps
    # daily, so same-day spend never exceeds the daily budget). ₹0 just means
    # the ad didn't deliver on that date (e.g. launched late / still scheduled).
    spend_map = {}
    for cid, dt, sp in conn.execute(
        'SELECT campaign_id, date, COALESCE(SUM(spend), 0) FROM meta_ads_daily '
        'WHERE date >= ? AND campaign_id IS NOT NULL GROUP BY campaign_id, date',
        (window_start,),
    ).fetchall():
        spend_map[(cid, dt)] = float(sp or 0)

    # Build one row per campaign that STARTED within the last DAYS_NEW_ADS days,
    # tagged with its start date. The client groups these into a date dropdown so
    # every day's batch is browsable date-wise (nothing is lost at midnight).
    out = []
    for cid, portal, name, daily, lifetime, start_time, stop_time in all_rows:
        sdate = (start_time or '')[:10]
        if not sdate or sdate < window_start or sdate > today:
            continue
        nm = name or ''
        d, l = float(daily or 0), float(lifetime or 0)
        bnote = ''
        if d > 0:
            bval, btype = round(d), 'daily'
        elif l > 0:
            days = _schedule_days(start_time, stop_time)
            if days >= 1:
                bval, btype = round(l / days), 'daily'
                bnote = f'₹{round(l):,} lifetime ÷ {days} din'
            else:
                bval, btype = round(l), 'lifetime'
        else:
            bval, btype = 0, 'adset'               # budget on the ad sets (not in DB)
        out.append({
            'campaign_id': cid,
            'portal': portal or '',
            'name': nm,
            'date': sdate,
            'budget_note': bnote,
            'category': derive_category_v2(nm),
            'product': derive_product_and_category(nm)[0],
            'budget_val': bval,
            'budget_type': btype,
            'spent_today': round(spend_map.get((cid, sdate), 0)),
        })

    # Per-ad success estimate (historical spend-weighted ROAS of the product).
    _annotate_scale_estimate(conn, out)

    # Default to TODAY (the operator thinks in calendar days — showing
    # yesterday's batch as "latest" is confusing). Today is empty until tonight's
    # push, then fills automatically. Other days stay browsable in the dropdown.
    dates = sorted({c['date'] for c in out} | {today}, reverse=True)
    default_date = today

    # Video links are the only per-camp API cost — fetch them just for the
    # default date's ads to keep the build fast.
    _annotate_video_links([c for c in out if c['date'] == default_date])

    out.sort(key=lambda r: (r['date'], r['category'], -r['spent_today']))
    return {'dates': dates, 'default_date': default_date, 'today': today,
            'camps': out}


def _annotate_budget_change(conn, today, camps):
    """Mutates each camp dict in-place, adding 'change_kind' and 'change_amt'.

    change_kind ∈ {'new','up','down','same'} — 'new' the first build that sees
    the campaign today, then up/down/same vs that first-of-day budget.
    """
    try:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS campaign_budget_history (
                date           TEXT NOT NULL,
                campaign_id    TEXT NOT NULL,
                first_budget   REAL,
                last_budget    REAL,
                first_recorded TEXT,
                last_recorded  TEXT,
                PRIMARY KEY (date, campaign_id)
            )
        ''')
    except Exception as e:  # noqa: BLE001
        print(f"   ⚠️  budget-history table unavailable ({e}) — skipping inc/dec")
        for c in camps:
            c['change_kind'], c['change_amt'] = 'new', 0
        return

    now = now_iso()
    for c in camps:
        cid, budget = c['campaign_id'], c['budget']
        try:
            cur = conn.execute(
                'INSERT OR IGNORE INTO campaign_budget_history'
                '(date, campaign_id, first_budget, last_budget, first_recorded, last_recorded)'
                ' VALUES (?,?,?,?,?,?)',
                (today, cid, budget, budget, now, now),
            )
            if cur.rowcount:                       # first sighting today
                c['change_kind'], c['change_amt'] = 'new', 0
                continue
            row = conn.execute(
                'SELECT first_budget FROM campaign_budget_history'
                ' WHERE date=? AND campaign_id=?', (today, cid)
            ).fetchone()
            first = float(row[0]) if row and row[0] is not None else budget
            conn.execute(
                'UPDATE campaign_budget_history SET last_budget=?, last_recorded=?'
                ' WHERE date=? AND campaign_id=?', (budget, now, today, cid)
            )
            delta = round(budget - first, 0)
            if delta > 0:
                c['change_kind'], c['change_amt'] = 'up', delta
            elif delta < 0:
                c['change_kind'], c['change_amt'] = 'down', delta
            else:
                c['change_kind'], c['change_amt'] = 'same', 0
        except Exception:  # noqa: BLE001
            c['change_kind'], c['change_amt'] = 'new', 0


# Success-estimate tunables. The estimate is the historical SPEND-WEIGHTED
# lifetime ROAS of the new ad's product (fallback category) — how profitably
# past ads of this kind actually performed, weighting bigger-spend ads more
# (so it reflects both ROAS and spend). Bands are calibrated to this account's
# product-level ROAS, which clusters ~1.0-1.5x — NOT the heatmap's per-cell
# bands. Only ads with real spend feed a bucket, and a bucket needs enough
# total historical spend to be trusted.
SCALE_SUCCESS_ROAS     = 1.35   # spend-weighted ROAS >= this -> Likely Success
SCALE_FAIL_ROAS        = 1.10   # < this -> Likely Fail; in between -> 50-50
SCALE_MIN_SPEND        = 1000   # per-ad floor to enter a bucket
SCALE_MIN_BUCKET_SPEND = 50000  # bucket needs this much total spend to trust


def _annotate_scale_estimate(conn, camps):
    """Adds 'est_kind' ('success'|'fail'|'uncertain'|'lowdata'), 'est_roas'
    (float or None), 'est_spend' (bucket total spend), 'est_n', 'est_basis'.

    For each new ad, looks up the SPEND-WEIGHTED lifetime ROAS of its product
    (fallback category) from meta_ads_meta — sum(revenue)/sum(spend) over past
    ads with real spend. Historical ads are classified by NAME with the SAME
    functions used for the new campaigns so the product/category keys line up.
    A bucket is only trusted once it has SCALE_MIN_BUCKET_SPEND of history.
    """
    by_product, by_category = {}, {}     # key -> [spend, revenue, n]
    try:
        rows = conn.execute(
            'SELECT ad_name, total_spend, total_revenue FROM meta_ads_meta'
        ).fetchall()
    except Exception:  # noqa: BLE001
        rows = []
    for ad_name, spend, revenue in rows:
        spend = float(spend or 0)
        if spend < SCALE_MIN_SPEND:
            continue
        revenue = float(revenue or 0)
        nm = ad_name or ''
        prod = derive_product_and_category(nm)[0]
        cat = derive_category_v2(nm)
        for key, table in ((prod, by_product), (cat, by_category)):
            slot = table.setdefault(key, [0.0, 0.0, 0])
            slot[0] += spend
            slot[1] += revenue
            slot[2] += 1

    def _classify(roas):
        if roas >= SCALE_SUCCESS_ROAS:  return 'success'
        if roas <  SCALE_FAIL_ROAS:     return 'fail'
        return 'uncertain'

    for c in camps:
        spend = rev = 0.0
        n = 0
        basis = 'lowdata'
        ps = by_product.get(c['product'])
        cs = by_category.get(c['category'])
        if ps and ps[0] >= SCALE_MIN_BUCKET_SPEND:
            spend, rev, n, basis = ps[0], ps[1], ps[2], 'product'
        elif cs and cs[0] >= SCALE_MIN_BUCKET_SPEND:
            spend, rev, n, basis = cs[0], cs[1], cs[2], 'category'
        if basis == 'lowdata' or spend <= 0:
            c['est_kind'], c['est_roas'], c['est_spend'], c['est_n'], c['est_basis'] = \
                'lowdata', None, 0, n, basis
        else:
            roas = round(rev / spend, 2)
            c['est_kind'], c['est_roas'], c['est_spend'], c['est_n'], c['est_basis'] = \
                _classify(roas), roas, round(spend), n, basis


def _annotate_video_links(camps):
    """Attach an inline-playable video to each new campaign.

    build_dashboard is otherwise DB-only, but tonight's new campaigns are few
    (~50) so a bounded live Meta lookup is cheap. For each campaign we pull its
    first ad's creative, then the video's direct mp4 `source` (which plays even
    for dark/unpublished ad posts that the public Facebook page link won't show)
    plus a permalink as an open-on-FB fallback.

    Sets c['video_src'] (mp4, for the in-dashboard player) and c['video_url']
    (FB permalink fallback). Best-effort: token-gated, fails fast on rate limits
    (max_retries=1), with a 3-error circuit breaker so a grumpy Meta API can
    never stall the otherwise DB-only build. The mp4 source is a signed URL that
    expires after some hours, but the dashboard rebuilds every ~10 min so links
    stay fresh.
    """
    if not os.getenv('META_ACCESS_TOKEN'):
        return
    fails = 0
    for c in camps:
        if fails >= 3:
            break
        try:
            d = meta_get(
                f"{GRAPH_API}/{c['campaign_id']}/ads",
                {'fields': 'preview_shareable_link,'
                           'creative{video_id,object_story_spec,'
                           'effective_object_story_id}',
                 'limit': 1},
                max_retries=1, initial_backoff=2,
            )
        except (MetaRateLimitError, Exception):  # noqa: BLE001
            fails += 1
            continue
        ads = (d or {}).get('data') or []
        if not ads:
            continue
        ad = ads[0]
        cr = ad.get('creative') or {}
        story = cr.get('effective_object_story_id')
        if story:
            c['video_url'] = f"https://www.facebook.com/{story}"
        elif ad.get('preview_shareable_link'):
            c['video_url'] = ad['preview_shareable_link']
        vid = cr.get('video_id') or ((cr.get('object_story_spec') or {})
                                     .get('video_data') or {}).get('video_id')
        if not vid:
            continue
        try:
            v = meta_get(f"{GRAPH_API}/{vid}",
                         {'fields': 'source,permalink_url'},
                         max_retries=1, initial_backoff=2)
        except (MetaRateLimitError, Exception):  # noqa: BLE001
            fails += 1
            continue
        if v.get('source'):
            c['video_src'] = v['source']
        if v.get('permalink_url') and not c.get('video_url'):
            p = v['permalink_url']
            c['video_url'] = ('https://www.facebook.com' + p) if p.startswith('/') else p


def fetch_adsets(conn, since: str, until: str):
    """Adset metadata (name + audience inclusions/exclusions) for any adset
    that had spend in the period. Used by the Categories page drill-down
    to show camp structure including audience targeting.

    Returns an empty dict if the meta_adsets table hasn't been created
    yet (first deploy after the schema change, before db_init runs).
    """
    has_table = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='meta_adsets'"
    ).fetchone()
    if not has_table:
        return {}
    rows = conn.execute('''
        SELECT a.adset_id, a.portal, a.campaign_id, a.name,
               a.audiences_incl, a.audiences_excl, a.targeting_summary
        FROM meta_adsets a
        WHERE a.adset_id IN (
            SELECT DISTINCT adset_id FROM meta_ads_daily
            WHERE date BETWEEN ? AND ? AND adset_id IS NOT NULL
        )
    ''', (since, until)).fetchall()
    out = {}
    for r in rows:
        aid, portal, camp, name, incl, excl, summary = r
        out[aid] = {
            'portal': portal,
            'campaign_id': camp,
            'name': name,
            'audiences_incl': incl or '',
            'audiences_excl': excl or '',
            'targeting_summary': summary or '',
        }
    return out


def fetch_shopify_daily(conn, since: str, until: str):
    """Per-(portal, date) Shopify aggregates: real orders + real revenue.
    These are the GROUND TRUTH numbers — Meta's purchases/revenue from
    meta_ads_daily are pixel/CAPI-attributed and inflated by 2-3x in this
    account. Surfacing both lets the user see Pixel ROAS vs Real ROAS.

    Excludes cancelled orders. Uses created_at::date in the configured TZ
    (assumed IST since dates in meta_ads_daily are also IST-anchored).
    """
    rows = conn.execute('''
        SELECT
            portal,
            substr(created_at, 1, 10) AS date,
            COUNT(*) AS orders,
            SUM(COALESCE(total_price, 0)) AS revenue
        FROM shopify_orders
        WHERE substr(created_at, 1, 10) BETWEEN ? AND ?
          AND cancelled_at IS NULL
        GROUP BY portal, substr(created_at, 1, 10)
        ORDER BY portal, date
    ''', (since, until)).fetchall()
    out = []
    for portal, date, orders, revenue in rows:
        out.append({
            'portal':  portal,
            'date':    date,
            'orders':  int(orders or 0),
            'revenue': round(float(revenue or 0), 2),
        })
    return out


def fetch_active_snapshot(conn):
    """Return the latest live ACTIVE-status snapshot.

    {
      'time': '2026-06-02T15:28:00+05:30',
      'by_portal':  {'SM': {'active_camps': N, 'active_ads': M}, ...},
      'by_account': [{'portal':..,'account_id':..,'account_name':..,
                      'active_camps':N, 'active_ads':M}, ...]
    }

    Returns empty shape if the snapshot table doesn't exist yet (first
    deploy before db_init runs) or is empty. The frontend falls back to
    the per-ad effective_status filter in that case.
    """
    has_table = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name='meta_active_snapshot'"
    ).fetchone()
    if not has_table:
        return {'time': None, 'by_portal': {}, 'by_account': []}

    row = conn.execute(
        'SELECT MAX(snapshot_time) FROM meta_active_snapshot'
    ).fetchone()
    if not row or not row[0]:
        return {'time': None, 'by_portal': {}, 'by_account': []}
    latest = row[0]
    rows = conn.execute('''
        SELECT portal, account_id, account_name, active_camps, active_ads
        FROM meta_active_snapshot WHERE snapshot_time = ?
    ''', (latest,)).fetchall()
    by_portal = {}
    by_account = []
    for portal, aid, aname, cc, ac in rows:
        bp = by_portal.setdefault(portal, {'active_camps': 0, 'active_ads': 0})
        bp['active_camps'] += int(cc or 0)
        bp['active_ads']   += int(ac or 0)
        by_account.append({
            'portal': portal, 'account_id': aid, 'account_name': aname,
            'active_camps': int(cc or 0), 'active_ads': int(ac or 0),
        })
    return {'time': latest, 'by_portal': by_portal, 'by_account': by_account}


def fetch_active_snapshot_daily(conn, days_back: int = 30):
    """Return active-status snapshots aggregated per IST date — the LAST
    snapshot of each day. Used by the dashboard to plot a 30-day history
    of active camps/ads, so the operator can see launches/pauses as a trend.

    Shape:
      [
        {'date': '2026-06-04',
         'time': '2026-06-04T22:30:00+05:30',
         'total':     {'active_camps': N, 'active_ads': M},
         'by_portal': {'SM': {...}, 'SML': {...}, 'NBP': {...}}},
        ...sorted oldest -> newest
      ]

    Empty list if the snapshot table doesn't exist yet.
    """
    has_table = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name='meta_active_snapshot'"
    ).fetchone()
    if not has_table:
        return []

    # For each IST date, find the LAST snapshot_time and use it as the
    # canonical "end-of-day active state". Limit to last `days_back` days.
    rows = conn.execute('''
        WITH per_day AS (
          SELECT date(substr(snapshot_time, 1, 10)) AS d,
                 MAX(snapshot_time) AS last_t
          FROM meta_active_snapshot
          WHERE snapshot_time >= date('now', ?)
          GROUP BY d
        )
        SELECT p.d, p.last_t, s.portal,
               SUM(s.active_camps) AS camps,
               SUM(s.active_ads)   AS ads
        FROM per_day p
        JOIN meta_active_snapshot s ON s.snapshot_time = p.last_t
        GROUP BY p.d, p.last_t, s.portal
        ORDER BY p.d ASC, s.portal ASC
    ''', (f'-{days_back} days',)).fetchall()

    by_date = {}
    for d, last_t, portal, camps, ads in rows:
        bucket = by_date.setdefault(d, {'date': d, 'time': last_t,
                                        'total': {'active_camps': 0, 'active_ads': 0},
                                        'by_portal': {}})
        bucket['by_portal'][portal] = {'active_camps': int(camps or 0),
                                       'active_ads':   int(ads or 0)}
        bucket['total']['active_camps'] += int(camps or 0)
        bucket['total']['active_ads']   += int(ads or 0)
    return sorted(by_date.values(), key=lambda x: x['date'])


def fetch_freshness(conn):
    out = {}
    for r in conn.execute('''
        SELECT job_name, target_date, status, started_at, finished_at, rows_written
        FROM ingest_log
        WHERE (job_name, started_at) IN (
          SELECT job_name, MAX(started_at) FROM ingest_log GROUP BY job_name
        )
    ''').fetchall():
        out[r[0]] = {
            'target_date': r[1], 'status': r[2],
            'started_at': r[3], 'finished_at': r[4],
            'rows_written': r[5],
        }
    return out


# Main HTML — kept in a separate function to avoid massive escape soup
def render_html(payload_json: str, since: str, until: str) -> str:
    return HTML_TEMPLATE.replace('__PAYLOAD__', payload_json).replace(
        '__SINCE__', since).replace('__UNTIL__', until)



HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>NTN Analytics</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
* { margin:0; padding:0; box-sizing:border-box; }
body { font-family:-apple-system,'Segoe UI',Roboto,sans-serif; background:#f0f4fb; color:#1a1a2e; font-size:13px; }
a { color:#1a3d7c; text-decoration:none; }

/* Sidebar */
.sidebar { position:fixed; top:0; left:0; height:100vh; width:220px; background:#0d2145; color:#fff; padding:18px 0; overflow-y:auto; box-shadow:2px 0 8px rgba(0,0,0,.08); z-index:100; }
.sidebar-brand { padding:0 18px 14px; border-bottom:1px solid rgba(255,255,255,.1); margin-bottom:10px; }
.sidebar-brand h1 { font-size:15px; font-weight:800; }
.sidebar-brand p { font-size:10px; color:rgba(255,255,255,.5); margin-top:2px; }
.menu { list-style:none; }
.menu li a { display:flex; align-items:center; gap:10px; padding:10px 18px; color:rgba(255,255,255,.75); font-size:12px; font-weight:600; cursor:pointer; border-left:3px solid transparent; transition:all .12s; }
.menu li a:hover { background:rgba(255,255,255,.05); color:#fff; }
.menu li a.active { background:rgba(255,255,255,.08); color:#fff; border-left-color:#5b8def; font-weight:800; }
.menu-icon { font-size:14px; width:18px; text-align:center; }
.menu-section-lbl { padding:14px 18px 6px; font-size:9px; font-weight:800; text-transform:uppercase; letter-spacing:.6px; color:rgba(255,255,255,.4); }
.sidebar-footer { position:absolute; bottom:0; left:0; right:0; padding:14px 18px; border-top:1px solid rgba(255,255,255,.1); font-size:9px; color:rgba(255,255,255,.4); }
.sidebar-footer a { color:rgba(255,255,255,.6); }

/* Main wrapper */
.wrap { margin-left:220px; min-height:100vh; }

/* Top filter bar */
.topbar { background:#fff; padding:12px 20px; border-bottom:1px solid #dde3f0; position:sticky; top:0; z-index:50; box-shadow:0 1px 4px rgba(0,0,0,.04); }
.topbar-row { display:flex; gap:14px; flex-wrap:wrap; align-items:center; }
.ctrl-group { display:flex; flex-direction:column; gap:3px; }
.ctrl-lbl { font-size:9px; font-weight:700; text-transform:uppercase; letter-spacing:.5px; color:#6b7280; }
.ctrl-input, .ctrl-select { padding:5px 9px; border:1px solid #dde3f0; border-radius:6px; font-size:12px; min-width:120px; background:#fff; }
.preset-btns { display:flex; gap:4px; }
.preset-btn { padding:5px 10px; border:1px solid #dde3f0; background:#fff; border-radius:6px; font-size:11px; cursor:pointer; font-weight:600; }
.preset-btn.active { background:#1a3d7c; color:#fff; border-color:#1a3d7c; }
.btn-clear { background:#fff5f5; color:#a3260a; border:1px solid #fed7d7; padding:5px 10px; border-radius:6px; font-size:11px; cursor:pointer; font-weight:700; }
.multi-sel { display:flex; gap:4px; flex-wrap:wrap; max-width:280px; }
.chip { padding:3px 8px; border-radius:14px; background:#eef2ff; color:#1a3d7c; font-size:10px; font-weight:700; cursor:pointer; border:1px solid #dde3f0; }
.chip.active { background:#1a3d7c; color:#fff; border-color:#1a3d7c; }

/* Page content */
.page-area { padding:18px 20px; max-width:1700px; }
.page { display:none; }
.page.active { display:block; }
.page h2 { font-size:18px; font-weight:800; color:#0d2145; margin-bottom:5px; }
.page h2 .subtle { font-weight:400; font-size:12px; color:#6b7280; margin-left:8px; }
.page-intro { color:#6b7280; margin-bottom:14px; font-size:12px; }

/* KPI cards */
.kpi-strip { display:grid; grid-template-columns:repeat(auto-fit,minmax(170px,1fr)); gap:10px; margin-bottom:18px; }
.kpi-card { background:#fff; padding:12px 14px; border-radius:10px; border:1px solid #dde3f0; position:relative; overflow:hidden; }
.kpi-card.kpi-good::before { content:''; position:absolute; top:0; left:0; width:3px; height:100%; background:#0d6e3a; }
.kpi-card.kpi-warn::before { content:''; position:absolute; top:0; left:0; width:3px; height:100%; background:#a35a00; }
.kpi-card.kpi-bad::before  { content:''; position:absolute; top:0; left:0; width:3px; height:100%; background:#a3260a; }
.kpi-card.kpi-good .kpi-val { color:#0d6e3a; }
.kpi-card.kpi-warn .kpi-val { color:#a35a00; }
.kpi-card.kpi-bad  .kpi-val { color:#a3260a; }
.kpi-lbl { font-size:9px; text-transform:uppercase; letter-spacing:.5px; color:#6b7280; font-weight:700; }
.kpi-val { font-size:22px; font-weight:800; color:#0d2145; margin-top:2px; }
.kpi-sub { font-size:10px; color:#6b7280; margin-top:2px; }
.delta-up { color:#059669; font-weight:700; }
.delta-down { color:#dc2626; font-weight:700; }

/* Cards/sections within a page */
.card { background:#fff; border-radius:10px; padding:14px 16px; border:1px solid #dde3f0; margin-bottom:14px; }
.card h3 { font-size:13px; font-weight:800; color:#0d2145; margin-bottom:10px; padding-bottom:6px; border-bottom:1px solid #eef2ff; display:flex; align-items:center; justify-content:space-between; }
.card h3 .meta { font-weight:400; font-size:10px; color:#6b7280; }

.charts-row { display:grid; grid-template-columns:1fr 1fr; gap:14px; }
@media (max-width:1100px) { .charts-row { grid-template-columns:1fr; } }
.chart-wrap { position:relative; height:240px; }

/* Tables */
table { width:100%; border-collapse:collapse; font-size:11px; }
th { background:#f8faff; color:#374151; padding:7px 9px; text-align:left; font-size:10px; text-transform:uppercase; letter-spacing:.4px; border-bottom:1px solid #dde3f0; cursor:pointer; user-select:none; white-space:nowrap; }
th:not(:first-child) { text-align:right; }
th.sorted-asc::after { content:' ↑'; color:#1a3d7c; }
th.sorted-desc::after { content:' ↓'; color:#1a3d7c; }
td { padding:7px 9px; border-bottom:1px solid #f3f4f6; }
td:not(:first-child) { text-align:right; font-variant-numeric:tabular-nums; }
tr:hover td { background:#fafbff; }
.rg { color:#059669; font-weight:700; }
.ro { color:#d97706; font-weight:700; }
.rr { color:#dc2626; font-weight:700; }
.tag { display:inline-block; padding:2px 7px; border-radius:5px; font-size:10px; font-weight:700; }
.tag-sm  { background:#dbeafe; color:#1d4ed8; }
.tag-sml { background:#d1fae5; color:#065f46; }
.tag-nbp { background:#fef3c7; color:#92400e; }
.subtle { color:#9ca3af; font-size:10px; }
.empty { padding:30px 14px; text-align:center; color:#9ca3af; font-style:italic; }
.cell-name { max-width:340px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.btn-csv { background:#1a3d7c; color:#fff; border:none; padding:6px 11px; border-radius:6px; font-size:10px; cursor:pointer; font-weight:700; }
.btn-csv:hover { background:#0d2145; }

/* Aaj-ki-nayi-ads detail table: text cols left; pushed/spent/estimate right */
#tbl-newtoday-detail th, #tbl-newtoday-detail td { text-align:left; }
#tbl-newtoday-detail th:nth-child(5), #tbl-newtoday-detail td:nth-child(5),
#tbl-newtoday-detail th:nth-child(6), #tbl-newtoday-detail td:nth-child(6),
#tbl-newtoday-detail th:nth-child(7), #tbl-newtoday-detail td:nth-child(7) { text-align:right; }

/* Heatmap success cells */
.sr-0 { background:#fef2f2; color:#7f1d1d; padding:2px 6px; border-radius:4px; }
.sr-low { background:#fef3c7; color:#78350f; padding:2px 6px; border-radius:4px; }
.sr-med { background:#dcfce7; color:#14532d; padding:2px 6px; border-radius:4px; }
.sr-hi { background:#bbf7d0; color:#065f46; font-weight:700; padding:2px 6px; border-radius:4px; }
</style>
</head>
<body>

<aside class="sidebar">
  <div class="sidebar-brand">
    <h1>📊 NTN Analytics</h1>
    <p>SQLite-backed · Hourly refresh</p>
  </div>

  <ul class="menu">
    <li class="menu-section-lbl">Performance</li>
    <li><a data-page="overview" class="active"><span class="menu-icon">📊</span>Overview</a></li>
    <li><a data-page="trends"><span class="menu-icon">📈</span>Trends</a></li>

    <li class="menu-section-lbl">Breakdowns</li>
    <li><a data-page="categories"><span class="menu-icon">📂</span>Categories</a></li>
    <li><a data-page="creatives"><span class="menu-icon">🎨</span>Creative Types</a></li>
    <li><a data-page="sentiments"><span class="menu-icon">💬</span>Sentiments</a></li>
    <li><a data-page="heatmap"><span class="menu-icon">🔥</span>Heatmap</a></li>

    <li class="menu-section-lbl">Drill-Down</li>
    <li><a data-page="prodbudget"><span class="menu-icon">💰</span>Product Budget</a></li>
    <li><a data-page="products"><span class="menu-icon">🛍️</span>Products</a></li>
    <li><a data-page="prodsuccess"><span class="menu-icon">🎯</span>Product Success</a></li>
    <li><a data-page="topads"><span class="menu-icon">🏆</span>Top Ads</a></li>
    <li><a data-page="bottomads"><span class="menu-icon">🥶</span>Bottom Ads</a></li>

    <li class="menu-section-lbl">Other Dashboards</li>
    <li><a href="/" target="_self"><span class="menu-icon">🏠</span>NTN Home</a></li>
    <li><a href="/today_live.html" target="_self"><span class="menu-icon">🔴</span>Today Live</a></li>
    <li><a href="/categories" target="_self"><span class="menu-icon">📁</span>Old Categories</a></li>
  </ul>

  <div class="sidebar-footer" id="sidebar-footer">Loading…</div>
</aside>

<div class="wrap">

  <!-- Top filter bar (sticky, applies to all pages) -->
  <div class="topbar">
    <div class="topbar-row">
      <div class="ctrl-group">
        <span class="ctrl-lbl">Date Range</span>
        <div class="preset-btns" id="preset-btns">
          <button class="preset-btn" data-days="1">Today</button>
          <button class="preset-btn" data-days="3">3D</button>
          <button class="preset-btn active" data-days="7">7D</button>
          <button class="preset-btn" data-days="14">14D</button>
          <button class="preset-btn" data-days="30">30D</button>
          <button class="preset-btn" data-days="all">All</button>
        </div>
      </div>
      <div class="ctrl-group">
        <span class="ctrl-lbl">From</span>
        <input type="date" class="ctrl-input" id="from-date">
      </div>
      <div class="ctrl-group">
        <span class="ctrl-lbl">To</span>
        <input type="date" class="ctrl-input" id="to-date">
      </div>
      <div class="ctrl-group">
        <span class="ctrl-lbl">Portal</span>
        <div class="multi-sel" id="filter-portals"></div>
      </div>
      <div class="ctrl-group">
        <span class="ctrl-lbl">Category</span>
        <div class="multi-sel" id="filter-categories"></div>
      </div>
      <div class="ctrl-group">
        <span class="ctrl-lbl">Creative</span>
        <div class="multi-sel" id="filter-creatives"></div>
      </div>
      <div class="ctrl-group" style="flex:1;min-width:280px;max-width:520px">
        <span class="ctrl-lbl">Product · families <span style="color:#9ca3af;font-weight:400">(type to search · click multiple)</span></span>
        <input type="text" class="ctrl-input" id="product-search" placeholder="🔎 type product name..." style="margin-bottom:4px;width:100%">
        <div class="multi-sel" id="filter-products" style="max-height:120px;overflow-y:auto"></div>
      </div>
      <div class="ctrl-group">
        <span class="ctrl-lbl">&nbsp;</span>
        <button class="btn-clear" id="btn-clear">Clear Filters</button>
      </div>
      <div class="ctrl-group" style="margin-left:auto">
        <span class="ctrl-lbl">Data refreshed</span>
        <div id="last-updated-pill" style="font-size:11px;font-weight:700;padding:5px 11px;border-radius:6px;white-space:nowrap;border:1px solid transparent"></div>
      </div>
      <div class="ctrl-group">
        <span class="ctrl-lbl">&nbsp;</span>
        <button id="btn-refresh-now" type="button" title="Force a fresh ingest + rebuild now (~10 min). Use this when the freshness pill is yellow/red."
                style="background:#1a3d7c;color:#fff;border:none;padding:6px 13px;border-radius:6px;font-size:11px;cursor:pointer;font-weight:700;white-space:nowrap">🔄 Refresh now</button>
      </div>
    </div>
  </div>

  <div class="page-area">

    <!-- ── PAGE: Overview ─────────────────────────────────────────── -->
    <section class="page active" id="page-overview">
      <h2>📊 Overview <span class="subtle" id="ov-meta"></span></h2>
      <p class="page-intro">Top-line KPIs and high-level breakdown for the selected period. Use sidebar to drill deeper.</p>

      <!-- Single Shopify-reality KPI strip. Pixel-attributed cards removed
           per user direction — Meta's pixel was inflating orders/revenue by
           ~1.8x and confusing primary read. Shopify Admin API is ground
           truth. The Spend / CPM / CTR / Active Ads cards stay because
           they're ad-mechanics, not attribution. -->
      <div id="range-pill" style="display:inline-block;background:#1a3d7c;color:#fff;font-size:13px;font-weight:700;padding:6px 14px;border-radius:18px;margin-bottom:12px"></div>
      <div class="kpi-strip" id="kpi-strip-shopify"></div>

      <div class="card">
        <h3>📡 Active Ads & Camps · Daily Trend <span class="meta">last snapshot of each day · live Meta API counts (= Ads Manager)</span></h3>
        <table id="tbl-active-daily" class="sortable">
          <thead><tr>
            <th>Date</th>
            <th>SM Camps</th><th>SM Ads</th>
            <th>SML Camps</th><th>SML Ads</th>
            <th>NBP Camps</th><th>NBP Ads</th>
            <th>Total Camps</th><th>Total Ads</th>
          </tr></thead>
          <tbody></tbody>
        </table>
      </div>

      <div class="card">
        <h3>📈 Spend & ROAS by Day <span class="meta">spend bars + ROAS line · spend-weighted, current filter</span></h3>
        <div class="chart-wrap" style="height:340px"><canvas id="chart-spend-roas-combined"></canvas></div>
      </div>
      <div class="card">
        <h3>📊 Spend vs Revenue · Daily ROAS <span class="meta">side-by-side detail</span></h3>
        <div class="charts-row">
          <div class="chart-wrap"><canvas id="chart-spend-rev"></canvas></div>
          <div class="chart-wrap"><canvas id="chart-roas"></canvas></div>
        </div>
      </div>

      <div class="card">
        <h3>📂 Top Categories</h3>
        <table id="tbl-cat-mini">
          <thead><tr><th>Category</th><th>Active Ads</th><th>Spend</th><th>Revenue</th><th>ROAS</th></tr></thead>
          <tbody></tbody>
        </table>
      </div>
    </section>

    <!-- ── PAGE: Trends ───────────────────────────────────────────── -->
    <section class="page" id="page-trends">
      <h2>📈 Trends Over Time</h2>
      <p class="page-intro">Time-series view of spend, revenue, ROAS, and orders. <strong>Pick 3D or longer at the top</strong> — a single-day filter has nothing to trend over.</p>

      <div id="trend-single-day-hint" class="empty" style="display:none;padding:1rem;background:#fef3c7;border:1px solid #fbbf24;border-radius:6px;margin-bottom:1rem">
        ⚠ Current filter is a single day, so the trends below show only one point.
        Click <b>3D</b>, <b>7D</b>, or <b>30D</b> at the top of the page to see a real trend.
      </div>

      <div class="card">
        <h3>Spend vs Revenue</h3>
        <div class="chart-wrap" style="height:300px"><canvas id="chart-trend-spend-rev"></canvas></div>
      </div>
      <div class="card">
        <h3>Daily ROAS</h3>
        <div class="chart-wrap" style="height:280px"><canvas id="chart-trend-roas"></canvas></div>
      </div>
      <div class="card">
        <h3>Daily Orders</h3>
        <div class="chart-wrap" style="height:280px"><canvas id="chart-trend-orders"></canvas></div>
      </div>
      <div class="card">
        <h3>Day-by-Day Table</h3>
        <table id="tbl-daily">
          <thead><tr><th>Date</th><th>Active Ads</th><th>Spend</th><th>Revenue</th><th>Orders</th><th>ROAS</th><th>CPM</th><th>CTR</th></tr></thead>
          <tbody></tbody>
        </table>
      </div>
    </section>

    <!-- ── PAGE: Categories ───────────────────────────────────────── -->
    <section class="page" id="page-categories">
      <h2>📂 Per-Category Breakdown</h2>
      <p class="page-intro">All metrics by product category. Click any column header to sort.</p>

      <div class="card">
        <h3>🥧 Budget Allocation by Category <span class="meta">slice = sum of ACTIVE campaign daily budgets · tooltip shows spent + ROAS</span></h3>
        <div class="chart-wrap" style="height:360px"><canvas id="chart-cat-pie"></canvas></div>
        <div id="cat-pie-empty" class="empty" style="display:none">No ACTIVE campaigns with configured daily budget in this period.</div>
      </div>

      <!-- Drill-down: appears only when a Product is picked or a single Category chip is selected.
           Shows per-creative-type breakdown (Paras/Motion/Partnership/Static/Other) and the
           per-campaign breakdown for the selected slice. -->
      <div class="card" id="card-drilldown" style="display:none">
        <h3>🔍 Drill-down: <span id="drill-title" style="color:#1a3d7c"></span></h3>
        <div id="drill-summary" style="margin:6px 0 14px;font-size:13px;color:#475569"></div>

        <h4 style="margin:14px 0 6px;font-size:12px;color:#1a3d7c;letter-spacing:.6px;text-transform:uppercase">Creative-type split</h4>
        <table id="tbl-drill-ct">
          <thead><tr>
            <th data-col="type" data-type="str">Creative Type</th>
            <th data-col="ads" data-type="num">Active Ads</th>
            <th data-col="spend" data-type="num">Spend</th>
            <th data-col="orders" data-type="num">Orders</th>
            <th data-col="revenue" data-type="num">Revenue</th>
            <th data-col="roas" data-type="num">ROAS</th>
          </tr></thead>
          <tbody id="drill-ct-tbody"></tbody>
        </table>

        <h4 style="margin:22px 0 6px;font-size:12px;color:#1a3d7c;letter-spacing:.6px;text-transform:uppercase">Ad-set structure · audience inclusions & exclusions · rolling ROAS</h4>
        <table id="tbl-drill-adset">
          <thead><tr>
            <th data-col="campaign" data-type="str">Campaign → Ad-set</th>
            <th data-col="portal" data-type="str">Portal</th>
            <th data-col="incl" data-type="str">Audience incl.</th>
            <th data-col="excl" data-type="str">Audience excl.</th>
            <th data-col="ads" data-type="num">Ads</th>
            <th data-col="spend" data-type="num">Spend (window)</th>
            <th data-col="orders" data-type="num">Orders</th>
            <th data-col="roas" data-type="num">ROAS (window)</th>
            <th data-col="roas_today" data-type="num">Live ROAS (today)</th>
            <th data-col="roas_3d" data-type="num">3D ROAS</th>
            <th data-col="roas_7d" data-type="num">7D ROAS</th>
          </tr></thead>
          <tbody id="drill-adset-tbody"></tbody>
        </table>
      </div>

      <div class="card">
        <h3>📊 Spend & ROAS by Category <span class="meta">aggregated over selected window</span></h3>
        <div class="chart-wrap" style="height:280px"><canvas id="chart-cat-bar"></canvas></div>
      </div>
      <div class="card" id="card-cat-trend-spend">
        <h3>📈 Daily Spend by Category <span class="meta">one line per category, selected window</span></h3>
        <div class="chart-wrap" style="height:320px"><canvas id="chart-cat-trend-spend"></canvas></div>
        <div id="cat-trend-spend-hint" class="empty" style="display:none">Pick <b>3D</b> or longer to see daily category trends — a single-day window has no line to draw.</div>
      </div>
      <div class="card" id="card-cat-stack-spend">
        <h3>📊 Daily Spend Stack <span class="meta">each bar = one day, split by category</span></h3>
        <div class="chart-wrap" style="height:320px"><canvas id="chart-cat-stack-spend"></canvas></div>
        <div id="cat-stack-spend-hint" class="empty" style="display:none">Pick <b>3D</b> or longer to see the daily stack.</div>
      </div>
      <div class="card" id="card-cat-daily-table">
        <h3>📋 Daily Spend × Category <span class="meta">row = date · col = category · value = spend</span> <button class="btn-csv" onclick="exportCSV('catdaily')">↓ CSV</button></h3>
        <div style="overflow-x:auto"><table id="tbl-cat-daily"></table></div>
      </div>
      <div class="card" id="card-cat-trend-roas">
        <h3>🎯 Daily ROAS by Category <span class="meta">Meta-attributed (pixel)</span></h3>
        <div class="chart-wrap" style="height:320px"><canvas id="chart-cat-trend-roas"></canvas></div>
        <div id="cat-trend-roas-hint" class="empty" style="display:none">Pick <b>3D</b> or longer to see daily category trends.</div>
      </div>
      <div class="card">
        <h3>Full Table</h3>
        <table id="tbl-categories">
          <thead><tr>
            <th data-col="category" data-type="str">Category</th>
            <th data-col="active_ads" data-type="num">Active Ads</th>
            <th data-col="active_camps" data-type="num">Camps</th>
            <th data-col="spend" data-type="num">Spend (₹)</th>
            <th data-col="revenue" data-type="num">Revenue (₹)</th>
            <th data-col="purchases" data-type="num">Orders</th>
            <th data-col="roas" data-type="num">ROAS</th>
            <th data-col="cpm" data-type="num">CPM</th>
            <th data-col="ctr" data-type="num">CTR</th>
            <th data-col="atc_rate" data-type="num">ATC%</th>
            <th data-col="success_rate" data-type="num">Success%</th>
          </tr></thead>
          <tbody></tbody>
        </table>
      </div>
    </section>

    <!-- ── PAGE: Creative Types ───────────────────────────────────── -->
    <section class="page" id="page-creatives">
      <h2>🎨 Creative Types</h2>
      <p class="page-intro">Performance by creative origin: Paras, Wanda (AI), Partnership, Motion, Static, etc.</p>

      <div class="card">
        <h3>Comparison</h3>
        <div class="chart-wrap" style="height:280px"><canvas id="chart-ct-bar"></canvas></div>
      </div>
      <div class="card">
        <h3>Detail Table</h3>
        <table id="tbl-creatives">
          <thead><tr>
            <th data-col="creative_type" data-type="str">Type</th>
            <th data-col="active_ads" data-type="num">Ads</th>
            <th data-col="spend" data-type="num">Spend</th>
            <th data-col="revenue" data-type="num">Revenue</th>
            <th data-col="purchases" data-type="num">Orders</th>
            <th data-col="roas" data-type="num">ROAS</th>
            <th data-col="success_rate" data-type="num">Success%</th>
          </tr></thead>
          <tbody></tbody>
        </table>
      </div>
    </section>

    <!-- ── PAGE: Sentiments ───────────────────────────────────────── -->
    <section class="page" id="page-sentiments">
      <h2>💬 Sentiments × Creative Type</h2>
      <p class="page-intro">Sentiment codes (st1, st2…) come from <code>_st\d+_</code> tags in ad/campaign names. Update <code>sentiment_labels</code> table to set readable labels.</p>

      <div class="card">
        <h3>Sentiment Detail</h3>
        <table id="tbl-sentiment">
          <thead><tr>
            <th data-col="sentiment_code" data-type="str">Code</th>
            <th data-col="sentiment_name" data-type="str">Name</th>
            <th data-col="creative_type" data-type="str">Creative Type</th>
            <th data-col="active_ads" data-type="num">Ads</th>
            <th data-col="spend" data-type="num">Spend</th>
            <th data-col="revenue" data-type="num">Revenue</th>
            <th data-col="roas" data-type="num">ROAS</th>
          </tr></thead>
          <tbody></tbody>
        </table>
      </div>
    </section>

    <!-- ── PAGE: Heatmap ──────────────────────────────────────────── -->
    <section class="page" id="page-heatmap">
      <h2>🔥 Heatmap</h2>
      <p class="page-intro">Aaj ki nayi ads (category-wise budget) + Creative Type × Category ROAS heatmap.</p>

      <!-- Aaj ki nayi ads — campaigns that STARTED today (IST), category-wise
           daily budget. Independent of the date/portal filters above; resets
           at IST midnight on each hourly rebuild. -->
      <div class="card">
        <h3>🌙 Nayi Ads — Category-wise (date-wise) <span class="meta" id="newtoday-meta"></span></h3>
        <p class="page-intro" style="margin:0 0 8px">Kisi bhi din ki nayi ads chuno — category &amp; product wise, <strong>pushed budget</strong> (per-day) aur us din ka <strong>actual spend</strong>. Live Meta se, Ads Manager jaisa.</p>
        <div style="margin:0 0 12px">
          <label for="newtoday-date" style="font-size:11px;font-weight:700;color:#0d2145">📅 Date:&nbsp;</label>
          <select id="newtoday-date" style="padding:4px 9px;border:1px solid #dde3f0;border-radius:6px;font-size:12px;font-weight:600"></select>
        </div>
        <div class="kpi-strip" id="newtoday-kpis" style="margin-bottom:12px"></div>
        <table id="tbl-newtoday-rollup" style="margin-bottom:14px">
          <thead><tr><th>Category</th><th>Camps</th><th>Pushed (₹/day)</th><th>Spent (that day) ₹</th><th>Share %</th></tr></thead>
          <tbody></tbody>
        </table>
        <details>
          <summary style="cursor:pointer;font-weight:700;font-size:12px;color:#1a3d7c;margin:4px 0 10px">▼ Har ad ka detail (category → product → campaign)</summary>
          <div style="overflow-x:auto">
            <table id="tbl-newtoday-detail">
              <thead><tr><th>Category</th><th>Product</th><th>Campaign (Ad)</th><th>Portal</th><th title="The budget the operator set on the campaign. /day = daily budget, total = lifetime budget for the whole campaign. ad-set = budget is set on the ad sets (not at campaign level).">Pushed Budget</th><th>Spent (that day) ₹</th><th title="Will this new ad likely succeed? Based on the spend-weighted lifetime ROAS of past ads of this product/category (≥1.35x = success, 1.10–1.35x = 50-50, <1.10x = fail). A historical base-rate, not a guarantee.">Success Estimate ⓘ</th></tr></thead>
              <tbody></tbody>
            </table>
          </div>
        </details>
        <div style="font-size:11px;font-weight:700;color:#0d2145;margin:16px 0 6px;border-top:1px solid #eef2ff;padding-top:12px">📊 Spent today by category — split by success estimate</div>
        <div class="chart-wrap" id="newtoday-chart-wrap" style="height:300px"><canvas id="chart-newtoday"></canvas></div>
      </div>

      <div class="card" style="overflow-x:auto">
        <h3>🔥 Creative Type × Category Heatmap <span class="meta">Spend-weighted ROAS per cell · Green ≥2.5x, amber 1.5-2.5x, red &lt;1.5x</span></h3>
        <table id="tbl-heatmap"></table>
      </div>
    </section>

    <!-- ── PAGE: Product Budget ──────────────────────────────────── -->
    <section class="page" id="page-prodbudget">
      <h2>💰 Product Budget <span class="subtle">where Meta daily budget is allocated</span></h2>
      <p class="page-intro">Daily budget assigned at the Meta campaign level, rolled up by product. Only ACTIVE campaigns counted. Sorted descending — biggest budget first. Click any column header to re-sort.</p>

      <div class="kpi-strip" id="kpi-strip-prodbudget"></div>

      <div class="card">
        <h3>📊 Per-Product Daily Budget <span class="meta">rolled up across all active campaigns</span></h3>
        <table id="tbl-prodbudget">
          <thead><tr>
            <th data-col="product" data-type="str">Product</th>
            <th data-col="camps" data-type="num">Active Camps</th>
            <th data-col="daily_budget" data-type="num">Daily Budget</th>
            <th data-col="spend_today" data-type="num">Spend Today</th>
            <th data-col="utilization" data-type="num">Utilization %</th>
            <th data-col="spend_7d" data-type="num">Spend (7D)</th>
            <th data-col="roas_7d" data-type="num">7D ROAS</th>
            <th data-col="roas_window" data-type="num">ROAS (selected window)</th>
          </tr></thead>
          <tbody></tbody>
        </table>
      </div>

      <div class="card">
        <h3>📋 Per-Campaign Daily Budget <span class="meta">all active campaigns, top 100 by budget</span></h3>
        <table id="tbl-campbudget">
          <thead><tr>
            <th data-col="name" data-type="str">Campaign</th>
            <th data-col="product" data-type="str">Product</th>
            <th data-col="portal" data-type="str">Portal</th>
            <th data-col="daily_budget" data-type="num">Daily Budget</th>
            <th data-col="spend_today" data-type="num">Spend Today</th>
            <th data-col="utilization" data-type="num">Util %</th>
            <th data-col="spend_7d" data-type="num">Spend (7D)</th>
            <th data-col="roas_today" data-type="num">Live ROAS</th>
            <th data-col="roas_3d" data-type="num">3D ROAS</th>
            <th data-col="roas_7d" data-type="num">7D ROAS</th>
          </tr></thead>
          <tbody></tbody>
        </table>
      </div>
    </section>

    <!-- ── PAGE: Products ─────────────────────────────────────────── -->
    <section class="page" id="page-products">
      <h2>🛍️ Products <button class="btn-csv" onclick="exportCSV('products')">↓ CSV</button></h2>
      <p class="page-intro">Per-product performance with success-at-ROAS rates. Use the Product filter at top to drill into one.</p>

      <div class="card">
        <h3>Product Performance <span class="meta">success-at-roas % = of ads launched in period, fraction whose lifetime ROAS hit threshold</span></h3>
        <table id="tbl-products">
          <thead><tr>
            <th data-col="product" data-type="str">Product</th>
            <th data-col="category" data-type="str">Cat</th>
            <th data-col="active_ads" data-type="num">Ads</th>
            <th data-col="spend" data-type="num">Spend</th>
            <th data-col="revenue" data-type="num">Revenue</th>
            <th data-col="orders" data-type="num">Orders</th>
            <th data-col="roas" data-type="num">ROAS</th>
            <th data-col="hit_15" data-type="num">≥1.5x</th>
            <th data-col="hit_20" data-type="num">≥2.0x</th>
            <th data-col="hit_25" data-type="num">≥2.5x</th>
            <th data-col="hit_30" data-type="num">≥3.0x</th>
            <th data-col="hit_40" data-type="num">≥4.0x</th>
            <th data-col="hit_50" data-type="num">≥5.0x</th>
          </tr></thead>
          <tbody></tbody>
        </table>
      </div>
    </section>

    <!-- ── PAGE: Product Success Rate (campaign-level) ─────────────── -->
    <section class="page" id="page-prodsuccess">
      <h2>🎯 Product Success Rate <span class="subtle">campaign-level</span> <button class="btn-csv" onclick="exportCSV('prodsuccess')">↓ CSV</button></h2>
      <p class="page-intro">For each product: how many <strong>campaigns</strong> were published in the selected period, what their lifetime ROAS distribution looks like, and what % cleared each ROAS bar. Different from the Products page (which counts ads). Useful for "is this product worth scaling?" decisions.</p>

      <div class="card">
        <h3>Per-Product Campaign Success <span class="meta">campaigns w/ ≥ ₹500 spend in period · ROAS = period revenue / period spend per campaign</span></h3>
        <table id="tbl-prodsuccess">
          <thead><tr>
            <th data-col="product" data-type="str">Product</th>
            <th data-col="category" data-type="str">Cat</th>
            <th data-col="campaigns" data-type="num">Camps Pub.</th>
            <th data-col="spend" data-type="num">Spend</th>
            <th data-col="revenue" data-type="num">Revenue</th>
            <th data-col="orders" data-type="num">Orders</th>
            <th data-col="roas" data-type="num">Agg ROAS</th>
            <th data-col="best_roas" data-type="num">Best Camp</th>
            <th data-col="hit_15" data-type="num">≥1.5x</th>
            <th data-col="hit_20" data-type="num">≥2.0x</th>
            <th data-col="hit_25" data-type="num">≥2.5x</th>
            <th data-col="hit_30" data-type="num">≥3.0x</th>
            <th data-col="hit_40" data-type="num">≥4.0x</th>
            <th data-col="hit_50" data-type="num">≥5.0x</th>
          </tr></thead>
          <tbody></tbody>
        </table>
      </div>
    </section>

    <!-- ── PAGE: Top Ads ──────────────────────────────────────────── -->
    <section class="page" id="page-topads">
      <h2>🏆 Top Ads <button class="btn-csv" onclick="exportCSV('topads')">↓ CSV</button></h2>
      <p class="page-intro">Top 50 by ROAS. Min ₹2K spend filter to skip flukes.</p>
      <div class="card">
        <table id="tbl-topads">
          <thead><tr>
            <th data-col="portal" data-type="str">Portal</th>
            <th data-col="category" data-type="str">Cat</th>
            <th data-col="creative_type" data-type="str">Type</th>
            <th data-col="ad_name" data-type="str">Ad / Campaign</th>
            <th data-col="days_active" data-type="num">Days</th>
            <th data-col="spend" data-type="num">Spend</th>
            <th data-col="revenue" data-type="num">Revenue</th>
            <th data-col="orders" data-type="num">Orders</th>
            <th data-col="roas" data-type="num">ROAS</th>
          </tr></thead>
          <tbody></tbody>
        </table>
      </div>
    </section>

    <!-- ── PAGE: Bottom Ads ───────────────────────────────────────── -->
    <section class="page" id="page-bottomads">
      <h2>🥶 Bottom Ads <span class="subtle">kill candidates</span> <button class="btn-csv" onclick="exportCSV('bottomads')">↓ CSV</button></h2>
      <p class="page-intro">Worst 50 by ROAS, min ₹2K spend (avoids surfacing tiny test ads). These are kill candidates per your same-day kill protocol.</p>
      <div class="card">
        <table id="tbl-bottomads">
          <thead><tr>
            <th data-col="portal" data-type="str">Portal</th>
            <th data-col="category" data-type="str">Cat</th>
            <th data-col="creative_type" data-type="str">Type</th>
            <th data-col="ad_name" data-type="str">Ad / Campaign</th>
            <th data-col="days_active" data-type="num">Days</th>
            <th data-col="spend" data-type="num">Spend</th>
            <th data-col="revenue" data-type="num">Revenue</th>
            <th data-col="orders" data-type="num">Orders</th>
            <th data-col="roas" data-type="num">ROAS</th>
          </tr></thead>
          <tbody></tbody>
        </table>
      </div>
    </section>

  </div>
</div>

<script>
const PAYLOAD = __PAYLOAD__;
const RAW = PAYLOAD.rows;
const DIM = PAYLOAD.dimensions;
const FRESH = PAYLOAD.freshness;
document.getElementById('sidebar-footer').innerHTML =
  `Built ${PAYLOAD.updated_at.slice(0, 16)}<br>${RAW.length.toLocaleString()} ad-day rows · ${DIM.products.length} products · ${DIM.categories.length} categories`;

// Top-bar freshness pill — sticky and visible from any page. Color-coded
// by age so a stalled cron is obvious without checking GHA.
//   green  ≤ 75 min  (ingest runs hourly, ~8 min latency)
//   amber  75–180 min
//   red    > 180 min — almost certainly broken pipeline
function renderLastUpdated() {
  const t   = new Date(PAYLOAD.updated_at);
  const now = new Date();
  const diffMin = Math.max(0, Math.floor((now - t) / 60000));
  let rel;
  if (diffMin < 1)         rel = 'just now';
  else if (diffMin < 60)   rel = `${diffMin}m ago`;
  else if (diffMin < 1440) rel = `${Math.floor(diffMin/60)}h ${diffMin%60}m ago`;
  else                     rel = `${Math.floor(diffMin/1440)}d ago`;
  const istTime = t.toLocaleString('en-IN', {
    timeZone: 'Asia/Kolkata',
    hour: '2-digit', minute: '2-digit', hour12: false,
    day: '2-digit', month: 'short',
  });
  const pill = document.getElementById('last-updated-pill');
  let dot, bg, bd, fg;
  if (diffMin > 180) {
    dot = '🔴'; bg = '#fef2f2'; bd = '#fca5a5'; fg = '#991b1b';
  } else if (diffMin > 75) {
    dot = '🟡'; bg = '#fef3c7'; bd = '#fcd34d'; fg = '#92400e';
  } else {
    dot = '🟢'; bg = '#e6f7ec'; bd = '#b8e6c8'; fg = '#0d6e3a';
  }
  pill.style.background = bg;
  pill.style.borderColor = bd;
  pill.style.color = fg;
  pill.textContent = `${dot} ${rel} · ${istTime} IST`;
  pill.title = `Dashboard payload built at ${PAYLOAD.updated_at}\nv2-ingest runs hourly via Cloudflare Worker + GHA fallback`;
}
renderLastUpdated();
setInterval(renderLastUpdated, 30000);

// Auto-reload the page every 15 min so the operator sees fresh data
// without having to remember to refresh. v2-ingest + today-live run on
// their own hourly schedules, so each reload picks up whatever Cloudflare
// has freshest — including today's partial-day data as it accumulates.
setInterval(() => location.reload(), 15 * 60 * 1000);

// "Refresh now" button — fires the Cloudflare Worker pings that dispatch
// v2-ingest immediately, then today-live ~8 min later (after ingest finishes).
// User can also navigate away — both Worker pings are fire-and-forget, and
// the regular hourly schedule covers the gap if anything is missed.
const REFRESH_WORKER = 'https://meta-ads-cron-pinger.pulkit-studdmuffyn.workers.dev';
document.getElementById('btn-refresh-now').addEventListener('click', async (e) => {
  const btn = e.currentTarget;
  const origText = btn.textContent;
  btn.disabled = true; btn.style.opacity = '0.65';
  btn.textContent = '⏳ Queuing ingest...';
  try {
    const r = await fetch(`${REFRESH_WORKER}/ping-ingest`, { mode: 'cors' });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    btn.textContent = '✓ Refresh queued · ~10 min';
    btn.style.background = '#059669';
    // Auto-trigger deploy after ingest should be done (~8 min)
    setTimeout(async () => {
      try { await fetch(`${REFRESH_WORKER}/ping-deploy`, { mode: 'cors' }); } catch (_) {}
      btn.textContent = '🚀 Rebuild firing... reload in 5 min';
    }, 8 * 60 * 1000);
    // Re-enable + suggest reload after full cycle
    setTimeout(() => {
      btn.textContent = '🔁 Reload page for new data';
      btn.style.background = '#1a3d7c';
      btn.disabled = false; btn.style.opacity = '1';
      btn.onclick = () => location.reload();
    }, 13 * 60 * 1000);
  } catch (err) {
    btn.textContent = `✗ ${err.message} (retry)`;
    btn.style.background = '#dc2626';
    setTimeout(() => {
      btn.textContent = origText;
      btn.style.background = '#1a3d7c';
      btn.disabled = false; btn.style.opacity = '1';
    }, 4000);
  }
});

// ── Filter state ─────────────────────────────────────────────────────────
const F = {
  fromDate: PAYLOAD.since,
  toDate:   PAYLOAD.until,
  portals:  new Set(),
  categories: new Set(),
  creative_types: new Set(),
  product_families: new Set(),    // multi-select; was F.product (string)
};

// Collapse SKU variants into a "product family" so 'AM/PM Booster Kit [165ml]',
// 'AM PM Pigmentation Combo', 'AM PM 180ml' all bucket under 'am pm'. Used
// by the product filter and by drill-down lookups. Leaves auto-derived
// slugs (~prefixed) intact — they're already short identifiers.
const _PRODUCT_FAMILY_STOP = new Set([
  'the','a','an','of','for','with','and','&',
  'nuskhe','by','paras','studd','muffyn','sm','sml','nbp','ntn',
  'combo','combo:','kit','pack','set','bundle','bottle','jar',
]);
function productFamily(name) {
  if (!name) return '(no product tag)';
  if (name[0] === '~') {
    const slug = name.slice(1).split('_').slice(0, 2).join(' ');
    return '~' + slug;
  }
  let n = name.replace(/\s*[\(\[\{][^\)\]\}]*[\)\]\}]\s*/g, ' '); // strip (...)/[...]
  n = n.toLowerCase().replace(/[\/\-,]/g, ' ').replace(/\s+/g, ' ').trim();
  const tokens = n.split(' ').filter(t => t);
  const significant = tokens.filter(t => !_PRODUCT_FAMILY_STOP.has(t) && t.length > 1);
  const pick = significant.length >= 2 ? significant.slice(0, 2)
             : significant.length === 1 ? significant
             : tokens.slice(0, 2);
  return pick.join(' ') || name;
}

const fmt = {
  inr:  n => n == null ? '—' : '₹' + Math.round(n).toLocaleString('en-IN'),
  num:  n => n == null ? '—' : Math.round(n).toLocaleString('en-IN'),
  num1: n => n == null ? '—' : Number(n).toFixed(1),
  pct:  n => n == null ? '—' : Number(n).toFixed(1) + '%',
  roas: n => {
    if (n == null) return '—';
    const cls = n >= 2.5 ? 'rg' : n >= 1.5 ? 'ro' : 'rr';
    return `<span class="${cls}">${Number(n).toFixed(2)}x</span>`;
  },
  delta: (cur, prev) => {
    if (prev == null || prev === 0) return '';
    const pct = ((cur - prev) / prev) * 100;
    const cls = pct >= 0 ? 'delta-up' : 'delta-down';
    const arrow = pct >= 0 ? '▲' : '▼';
    return `<span class="${cls}">${arrow} ${Math.abs(pct).toFixed(1)}%</span>`;
  },
};
function tag(p) { return `<span class="tag tag-${(p||'').toLowerCase()}">${p||'?'}</span>`; }

// ── Filter UI population ────────────────────────────────────────────────
function buildChips(containerId, values, set) {
  const c = document.getElementById(containerId);
  c.innerHTML = '';
  values.forEach(v => {
    const chip = document.createElement('span');
    chip.className = 'chip';
    chip.textContent = v;
    chip.addEventListener('click', () => {
      if (set.has(v)) { set.delete(v); chip.classList.remove('active'); }
      else { set.add(v); chip.classList.add('active'); }
      apply();
    });
    c.appendChild(chip);
  });
}
buildChips('filter-portals',    DIM.portals,        F.portals);
buildChips('filter-categories', DIM.categories,     F.categories);
buildChips('filter-creatives',  DIM.creative_types, F.creative_types);

// Build product-family chips with type-to-search.
// Each chip = one family; count = SKUs grouped into it.
const _famCount = new Map();
DIM.products.forEach(p => {
  const fam = productFamily(p);
  _famCount.set(fam, (_famCount.get(fam) || 0) + 1);
});
const _famList = [...new Set(DIM.products.map(productFamily))].sort();
const prodChipContainer = document.getElementById('filter-products');
function renderProductChips(filterText) {
  const q = (filterText || '').trim().toLowerCase();
  prodChipContainer.innerHTML = '';
  let shown = 0;
  for (const fam of _famList) {
    if (q && !fam.toLowerCase().includes(q)) continue;
    const chip = document.createElement('span');
    chip.className = 'chip';
    if (F.product_families.has(fam)) chip.classList.add('active');
    const cnt = _famCount.get(fam) || 1;
    chip.textContent = cnt > 1 ? `${fam} (${cnt})` : fam;
    chip.title = `${fam} — ${cnt} SKU variant${cnt > 1 ? 's' : ''}`;
    chip.addEventListener('click', () => {
      if (F.product_families.has(fam)) { F.product_families.delete(fam); chip.classList.remove('active'); }
      else { F.product_families.add(fam); chip.classList.add('active'); }
      apply();
    });
    prodChipContainer.appendChild(chip);
    shown++;
    if (shown >= 200) break;   // cap so search-of-empty doesn't render 1000 chips
  }
  if (shown === 0) {
    prodChipContainer.innerHTML = '<span class="subtle" style="padding:6px">no products match</span>';
  }
}
renderProductChips('');
document.getElementById('product-search').addEventListener('input', e => {
  renderProductChips(e.target.value);
});

document.getElementById('from-date').value = F.fromDate;
document.getElementById('to-date').value   = F.toDate;
document.getElementById('from-date').addEventListener('change', e => { F.fromDate = e.target.value; clearActivePreset(); apply(); });
document.getElementById('to-date').addEventListener('change',   e => { F.toDate   = e.target.value; clearActivePreset(); apply(); });

// Format a Date as YYYY-MM-DD in LOCAL time. Using `.toISOString().slice(0,10)`
// is a TZ landmine — it converts to UTC first, which in IST (+05:30) bumps
// the date back by one for any date built from a "YYYY-MM-DDT00:00:00" string.
// That bug made "Today" produce a 2-day range and 7D produce 8 days etc.
const fmtLocalDate = d =>
  `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;

document.querySelectorAll('.preset-btn').forEach(b => {
  b.addEventListener('click', () => {
    document.querySelectorAll('.preset-btn').forEach(x => x.classList.remove('active'));
    b.classList.add('active');
    const days = b.dataset.days;
    if (days === 'all') {
      F.fromDate = PAYLOAD.since; F.toDate = PAYLOAD.until;
    } else {
      const end = new Date(PAYLOAD.until + 'T00:00:00');
      const n = parseInt(days, 10);
      const start = new Date(end); start.setDate(end.getDate() - (n - 1));
      F.fromDate = fmtLocalDate(start);
      F.toDate   = PAYLOAD.until;
    }
    document.getElementById('from-date').value = F.fromDate;
    document.getElementById('to-date').value   = F.toDate;
    apply();
  });
});
function clearActivePreset() {
  document.querySelectorAll('.preset-btn').forEach(x => x.classList.remove('active'));
}

document.getElementById('btn-clear').addEventListener('click', () => {
  F.portals.clear(); F.categories.clear(); F.creative_types.clear(); F.product_families.clear();
  // Clear search box + re-render full chip list
  const ps = document.getElementById('product-search'); if (ps) ps.value = '';
  renderProductChips('');
  document.querySelectorAll('.chip').forEach(c => c.classList.remove('active'));
  prodSel.value = '';
  apply();
});

// ── Sidebar routing ─────────────────────────────────────────────────────
// Single switchPage() function — both clicks and hashchange call this so
// DOM state is always consistent. Earlier version used `location.hash = page`
// inside the click handler, which fired `hashchange`, which called .click()
// on the same link, which re-entered the handler. In jsdom this caused
// the .active class to bounce; in Chrome it MAY work but is fragile.
// Switching to `history.replaceState` avoids the hashchange entirely.
function switchPage(page) {
  if (!page) page = 'overview';
  const link = document.querySelector('.menu a[data-page="' + page + '"]');
  const section = document.getElementById('page-' + page);
  if (!link || !section) return;
  document.querySelectorAll('.menu a').forEach(x => x.classList.remove('active'));
  link.classList.add('active');
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  section.classList.add('active');
  // Update URL hash without firing hashchange (no re-entry)
  if (location.hash !== '#' + page) {
    history.replaceState(null, '', '#' + page);
  }
  // Defer apply() so the browser computes layout for the just-shown
  // .page section before Chart.js measures its canvases. requestAnimation
  // Frame alone is sometimes not enough in Chrome — the canvas can still
  // read 0x0 on first paint. Belt-and-braces:
  //   1. rAF to wait for layout
  //   2. apply() to render charts
  //   3. window resize event so Chart.js (responsive:true) re-measures
  //      and redraws if it got the wrong size on instantiation
  const doApply = () => {
    apply();
    // Force Chart.js to re-measure any responsive charts that may have
    // been built on a 0x0 canvas. Tiny setTimeout so resize fires after
    // chart instances are in place.
    setTimeout(() => {
      try { window.dispatchEvent(new Event('resize')); } catch (_) {}
    }, 50);
  };
  if (typeof requestAnimationFrame === 'function') {
    requestAnimationFrame(doApply);
  } else {
    doApply();
  }
}

document.querySelectorAll('.menu a[data-page]').forEach(a => {
  a.addEventListener('click', (e) => {
    e.preventDefault();
    switchPage(a.dataset.page);
  });
});

function activatePageFromHash() {
  const hash = location.hash.replace('#', '') || 'overview';
  switchPage(hash);
}
window.addEventListener('hashchange', activatePageFromHash);

// ── Filter logic ────────────────────────────────────────────────────────
function applyFilters(rows) {
  return rows.filter(r => {
    if (r.date < F.fromDate || r.date > F.toDate) return false;
    if (F.portals.size && !F.portals.has(r.portal)) return false;
    if (F.categories.size && !F.categories.has(r.category)) return false;
    if (F.creative_types.size && !F.creative_types.has(r.creative_type)) return false;
    if (F.product_families.size && !F.product_families.has(productFamily(r.product))) return false;
    return true;
  });
}

function getCompareSet() {
  const start = new Date(F.fromDate + 'T00:00:00');
  const end   = new Date(F.toDate   + 'T00:00:00');
  const ms    = end - start;
  const prevEnd   = new Date(start); prevEnd.setDate(start.getDate() - 1);
  const prevStart = new Date(prevEnd); prevStart.setTime(prevEnd.getTime() - ms);
  const prevFrom = prevStart.toISOString().slice(0, 10);
  const prevTo   = prevEnd.toISOString().slice(0, 10);
  return RAW.filter(r => {
    if (r.date < prevFrom || r.date > prevTo) return false;
    if (F.portals.size && !F.portals.has(r.portal)) return false;
    if (F.categories.size && !F.categories.has(r.category)) return false;
    if (F.creative_types.size && !F.creative_types.has(r.creative_type)) return false;
    if (F.product_families.size && !F.product_families.has(productFamily(r.product))) return false;
    return true;
  });
}

// ── Aggregations ────────────────────────────────────────────────────────
// Live ACTIVE-status snapshot (matches Ads Manager). PAYLOAD.active_snapshot
// is captured by ingest_meta.py via Meta's summary=total_count endpoint —
// truth-source, independent of whether ads had spend in window.
function getActiveSnapshot() {
  const snap = (PAYLOAD.active_snapshot) || { time: null, by_portal: {} };
  const portals = F.portals.size ? [...F.portals] : Object.keys(snap.by_portal);
  let camps = 0, ads = 0;
  for (const p of portals) {
    const v = snap.by_portal[p];
    if (v) { camps += (v.active_camps || 0); ads += (v.active_ads || 0); }
  }
  return { camps, ads, time: snap.time };
}

function aggregate(rows, groupKey) {
  const map = new Map();
  for (const r of rows) {
    const k = groupKey ? r[groupKey] : '__total__';
    if (!map.has(k)) map.set(k, {
      key:k, active_ads:new Set(), active_camps:new Set(),
      spend:0, revenue:0, purchases:0, impressions:0, clicks:0,
      atc:0, lpv:0,
    });
    const a = map.get(k);
    // "Active Ads" = ads that had spend in the selected date window.
    // Window-aware (Today vs 3D vs 7D will give different counts as the
    // window expands). Effective_status is a snapshot of *now*, not a
    // per-day record — using it here would make Active Ads invariant
    // across windows, which is exactly the problem the operator hit
    // (3D showed the same count as Today). Spend-row presence is the
    // only signal we have that's actually date-stamped.
    a.active_ads.add(r.ad_id);
    if (r.campaign_id) a.active_camps.add(r.campaign_id);
    a.spend       += r.spend       || 0;
    a.revenue     += r.revenue     || 0;
    a.purchases   += r.purchases   || 0;
    a.impressions += r.impressions || 0;
    a.clicks      += r.clicks      || 0;
    a.atc         += r.add_to_cart || 0;
    a.lpv         += r.landing_page_views || 0;
  }
  return [...map.values()].map(a => ({
    key: a.key,
    active_ads: a.active_ads.size,
    active_camps: a.active_camps.size,
    spend: a.spend, revenue: a.revenue, purchases: a.purchases,
    impressions: a.impressions, clicks: a.clicks,
    atc: a.atc, lpv: a.lpv,
    roas: a.spend > 0 ? a.revenue / a.spend : 0,
    cpm:  a.impressions > 0 ? (a.spend / a.impressions) * 1000 : 0,
    ctr:  a.impressions > 0 ? (a.clicks / a.impressions) * 100 : 0,
    atc_rate: a.clicks > 0 ? (a.atc / a.clicks) * 100 : 0,
  }));
}

function successRateWindowDays() {
  // Length of the current date filter, in days (inclusive). Used for
  // labelling only — the metric below is ROAS-based, not window-based.
  const fromD = new Date(F.fromDate + 'T00:00:00');
  const toD   = new Date(F.toDate   + 'T00:00:00');
  return Math.max(1, Math.round((toD - fromD) / 86400000) + 1);
}

// % of ads in the current filter window with ROAS >= roasThreshold (default
// 1.0x = broke even or better). Aggregates each ad's spend+revenue across
// the window, then computes per-ad ROAS. Works for any window length —
// today, 3d, 7d, 30d — because it doesn't depend on calendar-time
// survival like the old definition did.
function successRateDetail(rows, roasThreshold = 1.0) {
  const adAgg = new Map();
  for (const r of rows) {
    if (!adAgg.has(r.ad_id)) adAgg.set(r.ad_id, { spend: 0, revenue: 0 });
    const a = adAgg.get(r.ad_id);
    a.spend   += (r.spend   || 0);
    a.revenue += (r.revenue || 0);
  }
  let total = 0, success = 0;
  for (const a of adAgg.values()) {
    if (a.spend <= 0) continue;
    total++;
    if ((a.revenue / a.spend) >= roasThreshold) success++;
  }
  return { total, success, rate: total > 0 ? (100 * success / total) : null };
}

// Back-compat wrapper — many callers just want the rate number.
function successRate(rows, roasThreshold = 1.0) {
  return successRateDetail(rows, roasThreshold).rate;
}

// Per-date Shopify totals respecting current date+portal filter.
// (Shopify can't be filtered by category/creative/product — those filters
// are silently ignored here and the daily totals stay portal-level.)
function shopifyTimeSeries() {
  const by = new Map();
  for (const r of (PAYLOAD.shopify_daily || [])) {
    if (r.date < F.fromDate || r.date > F.toDate) continue;
    if (F.portals.size && !F.portals.has(r.portal)) continue;
    if (!by.has(r.date)) by.set(r.date, { orders: 0, revenue: 0 });
    const b = by.get(r.date);
    b.orders  += r.orders;
    b.revenue += r.revenue;
  }
  return by;
}

function timeSeries(rows) {
  // Meta-side mechanics (spend, impressions, clicks, ad count) come from
  // the filtered ad-day rows. Revenue / orders / ROAS come from Shopify
  // (ground truth) — Meta pixel attribution is over-reporting ~1.8x.
  const by = new Map();
  for (const r of rows) {
    if (!by.has(r.date)) by.set(r.date, { spend:0, ads: new Set(), impressions:0, clicks:0 });
    const b = by.get(r.date);
    b.spend       += r.spend       || 0;
    b.impressions += r.impressions || 0;
    b.clicks      += r.clicks      || 0;
    b.ads.add(r.ad_id);
  }
  const shop = shopifyTimeSeries();
  const dates = new Set([...by.keys(), ...shop.keys()]);
  const sorted = [...dates].sort();
  return sorted.map(d => {
    const v = by.get(d)   || { spend:0, ads: new Set(), impressions:0, clicks:0 };
    const s = shop.get(d) || { orders: 0, revenue: 0 };
    return {
      date: d,
      active_ads: v.ads.size,
      spend: Math.round(v.spend),
      revenue: Math.round(s.revenue),  // Shopify ground truth
      orders: s.orders,                 // Shopify ground truth
      roas: v.spend > 0 ? +(s.revenue / v.spend).toFixed(2) : 0,  // Real ROAS
      cpm: v.impressions > 0 ? +((v.spend / v.impressions) * 1000).toFixed(2) : 0,
      ctr: v.impressions > 0 ? +((v.clicks / v.impressions) * 100).toFixed(2) : 0,
    };
  });
}

// ── Sort state per table ────────────────────────────────────────────────
const sortState = {};
function setupSort(tableId, defaultCol, defaultDir = 'desc') {
  sortState[tableId] = { col: defaultCol, dir: defaultDir };
  document.querySelectorAll(`#${tableId} thead th`).forEach(th => {
    th.addEventListener('click', () => {
      const col = th.dataset.col;
      const cur = sortState[tableId];
      if (cur.col === col) cur.dir = cur.dir === 'asc' ? 'desc' : 'asc';
      else { cur.col = col; cur.dir = 'desc'; }
      apply();
    });
  });
}
function applySortHeaders(tableId) {
  const { col, dir } = sortState[tableId];
  document.querySelectorAll(`#${tableId} thead th`).forEach(th => {
    th.classList.remove('sorted-asc', 'sorted-desc');
    if (th.dataset.col === col) th.classList.add('sorted-' + dir);
  });
}
function sortRows(rows, tableId) {
  const st = sortState[tableId];
  if (!st) return rows;
  const { col, dir } = st;
  const factor = dir === 'asc' ? 1 : -1;
  return rows.slice().sort((a, b) => {
    const va = a[col], vb = b[col];
    if (va == null) return 1; if (vb == null) return -1;
    if (typeof va === 'number') return (va - vb) * factor;
    return String(va).localeCompare(String(vb)) * factor;
  });
}

// ── Chart pool ──────────────────────────────────────────────────────────
let charts = {};
function destroyChart(id) { if (charts[id]) { charts[id].destroy(); delete charts[id]; } }

function lineChart(canvasId, ts, datasets, opts = {}) {
  destroyChart(canvasId);
  const el = document.getElementById(canvasId);
  if (!el) return;
  charts[canvasId] = new Chart(el, {
    type: 'line',
    data: { labels: ts.map(t => t.date.slice(5)), datasets },
    options: { responsive:true, maintainAspectRatio:false, ...opts },
  });
}
function barChart(canvasId, labels, datasets, opts = {}) {
  destroyChart(canvasId);
  const el = document.getElementById(canvasId);
  if (!el) return;
  // Chart.js v4 requires `type` at the TOP level of config — putting it
  // inside `options` is silently ignored. Pull it out of opts; default to
  // 'bar' since this helper is mostly used for bar charts.
  const { type, ...restOpts } = opts;
  charts[canvasId] = new Chart(el, {
    type: type || 'bar',
    data: { labels, datasets },
    options: { responsive:true, maintainAspectRatio:false, ...restOpts },
  });
}

function pieChart(canvasId, labels, datasets, opts = {}) {
  destroyChart(canvasId);
  const el = document.getElementById(canvasId);
  if (!el) return;
  charts[canvasId] = new Chart(el, {
    type: 'pie',
    data: { labels, datasets },
    options: { responsive:true, maintainAspectRatio:false, ...opts },
  });
}

// ── Page renderers ──────────────────────────────────────────────────────
// Compute Shopify (real) totals from PAYLOAD.shopify_daily, scoped to the
// current date filter and any selected portals. We can't filter Shopify by
// category/creative/product (no per-line attribution) — so the Shopify strip
// always shows portal-level totals; the user can still narrow by date+portal.
function aggregateShopify() {
  const shopify = (PAYLOAD.shopify_daily || []);
  let orders = 0, revenue = 0;
  for (const r of shopify) {
    if (r.date < F.fromDate || r.date > F.toDate) continue;
    if (F.portals.size && !F.portals.has(r.portal)) continue;
    orders  += r.orders;
    revenue += r.revenue;
  }
  return { orders, revenue };
}

function renderOverview(rows, prevRows) {
  // Meta-attributed (Pixel) KPI strip
  const a = aggregate(rows)[0] || { spend:0, revenue:0, purchases:0, active_ads:0, active_camps:0, roas:0, cpm:0, ctr:0 };
  const p = aggregate(prevRows)[0] || { spend:0, revenue:0, purchases:0, roas:0 };
  document.getElementById('ov-meta').textContent = `· ${F.fromDate} → ${F.toDate}`;

  // Range pill — make it unmistakable whether KPIs are a single day or a sum
  const fromD = new Date(F.fromDate + 'T00:00:00');
  const toD   = new Date(F.toDate   + 'T00:00:00');
  const ndays = Math.round((toD - fromD) / 86400000) + 1;
  const todayStr = PAYLOAD.until;
  let rangeLabel;
  if (F.fromDate === F.toDate) {
    rangeLabel = F.fromDate === todayStr
      ? `📅 TODAY (${F.fromDate}) — live, refreshes hourly`
      : `📅 ${F.fromDate} (single day)`;
  } else {
    rangeLabel = `📅 ${F.fromDate} → ${F.toDate} (${ndays} days, totals are SUMS over the window)`;
  }
  document.getElementById('range-pill').textContent = rangeLabel;

  // ── Shopify-reality KPI strip (single source of truth for orders/revenue/ROAS)
  // Pixel-attributed Meta numbers (purchases/revenue/ROAS) removed — they
  // inflate by ~1.8x and were causing the dashboard to read as "wrong".
  // Ad-mechanics metrics that don't depend on attribution (Active Ads, CPM,
  // CTR, Success Rate) stay alongside Shopify orders/revenue.
  const shop = aggregateShopify();
  const realROAS  = a.spend > 0 ? (shop.revenue / a.spend) : 0;
  const filterNote = (F.categories.size || F.creative_types.size || F.product_families.size)
    ? "⚠ Shopify can't filter by category/creative — portal-level total"
    : 'all portals · all orders';

  const cards = [
    // "Active Ads" KPI: prefer live snapshot (= Meta API summary=total_count,
    // matches Ads Manager). Falls back to per-ad row filter if the snapshot
    // table hasn't been populated yet (first deploy / ingest hasn't run).
    ...(() => {
      const snap = getActiveSnapshot();
      const ok   = !!snap.time;
      const ads  = ok ? snap.ads   : a.active_ads;
      const camps= ok ? snap.camps : a.active_camps;
      const sub  = ok ? (camps + ' campaigns · live @ ' + snap.time.slice(11,16) + ' IST')
                      : (camps + ' campaigns · ⚠ snapshot pending');
      return [{ l:'Active Ads', v: fmt.num(ads), s: sub }];
    })(),
    { l:'Meta Spend',     v: fmt.inr(a.spend),       s: fmt.delta(a.spend, p.spend) + ' vs prev' },
    { l:'Real Orders',    v: fmt.num(shop.orders),   s: filterNote },
    { l:'Real Revenue',   v: fmt.inr(shop.revenue),  s: filterNote },
    { l:'ROAS',           v: fmt.roas(realROAS),     s: 'Shopify rev ÷ Meta spend',  cls: realROAS >= 2 ? 'good' : (realROAS >= 1.5 ? 'warn' : 'bad') },
    { l:'CPM',            v: fmt.inr(a.cpm),         s: 'cost / 1k impr' },
    { l:'CTR',            v: fmt.pct(a.ctr),         s: 'click-through' },
    { l:`Success Rate (${successRateWindowDays()}d)`, v: (() => { const d = successRateDetail(rows); return d.rate == null ? '—' : fmt.pct(d.rate); })(), s: (() => { const d = successRateDetail(rows); return d.total === 0 ? 'no spending ads in filter' : `${d.success} of ${d.total} ads · ROAS ≥ 1.0x`; })() },
  ];
  document.getElementById('kpi-strip-shopify').innerHTML = cards.map(c =>
    `<div class="kpi-card${c.cls ? ' kpi-' + c.cls : ''}"><div class="kpi-lbl">${c.l}</div>` +
    `<div class="kpi-val">${c.v}</div><div class="kpi-sub">${c.s}</div></div>`
  ).join('');

  // Daily active-status trend (uses payload.active_snapshot_daily — one
  // row per day with the LAST snapshot of that day, broken down by portal)
  const dailySnap = PAYLOAD.active_snapshot_daily || [];
  const tbody = document.querySelector('#tbl-active-daily tbody');
  if (tbody) {
    if (!dailySnap.length) {
      tbody.innerHTML = `<tr><td colspan="9" style="text-align:center;color:#888;padding:12px">No snapshot history yet — first daily row will appear after the next ingest run.</td></tr>`;
    } else {
      // Show newest first
      const sorted = [...dailySnap].reverse();
      tbody.innerHTML = sorted.map(d => {
        const sm  = d.by_portal.SM  || {active_camps:0, active_ads:0};
        const sml = d.by_portal.SML || {active_camps:0, active_ads:0};
        const nbp = d.by_portal.NBP || {active_camps:0, active_ads:0};
        return `<tr>
          <td><strong>${d.date}</strong></td>
          <td>${fmt.num(sm.active_camps)}</td><td>${fmt.num(sm.active_ads)}</td>
          <td>${fmt.num(sml.active_camps)}</td><td>${fmt.num(sml.active_ads)}</td>
          <td>${fmt.num(nbp.active_camps)}</td><td>${fmt.num(nbp.active_ads)}</td>
          <td><strong>${fmt.num(d.total.active_camps)}</strong></td>
          <td><strong>${fmt.num(d.total.active_ads)}</strong></td>
        </tr>`;
      }).join('');
    }
  }

  // Mini charts
  if (document.getElementById('page-overview').classList.contains('active')) {
    const ts = timeSeries(rows);
    const labels = ts.map(t => t.date);

    // Primary chart: spend bars + ROAS line on dual axis. One picture,
    // answers "how much did I spend today and how did it perform?"
    // without scanning two separate plots.
    barChart('chart-spend-roas-combined', labels, [
      { label:'Spend (₹)',
        data: ts.map(t => Math.round(t.spend)),
        backgroundColor:'#1a3d7c',
        yAxisID:'y',
        order: 2,
      },
      { label:'ROAS',
        data: ts.map(t => +((t.roas || 0).toFixed(2))),
        borderColor:'#d97706',
        backgroundColor:'#d9770633',
        type:'line',
        yAxisID:'y1',
        tension:.25,
        pointRadius: 3,
        order: 1,
      },
    ], {
      plugins:{ legend:{ position:'bottom' }, tooltip:{ mode:'index', intersect:false } },
      interaction:{ mode:'index', intersect:false },
      scales:{
        y:{ beginAtZero:true, position:'left',
            title:{ display:true, text:'Spend (₹)' },
            ticks:{ callback:v => '₹' + (v >= 100000 ? (v/100000).toFixed(1)+'L' : (v >= 1000 ? (v/1000).toFixed(0)+'K' : v)) } },
        y1:{ beginAtZero:true, position:'right',
             title:{ display:true, text:'ROAS' },
             grid:{ drawOnChartArea:false },
             ticks:{ callback:v => v.toFixed(1) + 'x' } },
      },
    });

    // Detail charts (kept for users who want spend+revenue together,
    // or a standalone ROAS line without the spend bars).
    // Spend as bars + revenue as a line, same axis (both ₹) — when the
    // green line sits ABOVE the blue bars on a day, that day was
    // profitable; below means lost money. Easier to read than two
    // overlapping filled-area lines.
    barChart('chart-spend-rev', ts.map(t => t.date.slice(5)), [
      { label:'Spend (₹)',
        data: ts.map(t => t.spend),
        backgroundColor:'#1a3d7c',
        order: 2,
      },
      { label:'Revenue (Shopify ₹)',
        data: ts.map(t => t.revenue),
        borderColor:'#059669',
        backgroundColor:'#059669',
        type:'line',
        tension:.25,
        pointRadius: 3,
        fill: false,
        order: 1,
      },
    ], {
      plugins:{ title:{ display:true, text:'Spend vs Revenue (₹)' }, legend:{ position:'bottom' }, tooltip:{ mode:'index', intersect:false } },
      interaction:{ mode:'index', intersect:false },
      scales:{
        y:{ beginAtZero:true,
            ticks:{ callback:v => '₹' + (v >= 100000 ? (v/100000).toFixed(1)+'L' : (v >= 1000 ? (v/1000).toFixed(0)+'K' : v)) } },
      },
    });
    lineChart('chart-roas', ts, [
      { label:'ROAS', data: ts.map(t => t.roas), borderColor:'#d97706', backgroundColor:'#d9770633', fill:true, tension:.25 },
    ], { plugins:{ title:{ display:true, text:'Daily ROAS'} } });
  }

  // Mini category table
  const cats = aggregate(rows, 'category').filter(c => c.key).sort((a, b) => b.spend - a.spend).slice(0, 8);
  document.querySelector('#tbl-cat-mini tbody').innerHTML = cats.map(c =>
    `<tr><td><strong>${c.key}</strong></td><td>${fmt.num(c.active_ads)}</td>` +
    `<td>${fmt.inr(c.spend)}</td><td>${fmt.inr(c.revenue)}</td><td>${fmt.roas(c.roas)}</td></tr>`
  ).join('') || '<tr><td colspan="5" class="empty">No data.</td></tr>';
}

function renderTrends(rows) {
  if (!document.getElementById('page-trends').classList.contains('active')) return;
  const ts = timeSeries(rows);
  const labels = ts.map(t => t.date.slice(5));
  // Spend bars + Revenue line on same ₹ axis — matches the Overview view.
  // When the green line sits above a day's blue bar, that day was
  // profitable; below means it lost money.
  barChart('chart-trend-spend-rev', labels, [
    { label:'Spend (₹)',
      data: ts.map(t => t.spend),
      backgroundColor:'#1a3d7c',
      order: 2,
    },
    { label:'Revenue (Shopify ₹)',
      data: ts.map(t => t.revenue),
      borderColor:'#059669',
      backgroundColor:'#059669',
      type:'line',
      tension:.25,
      pointRadius: 3,
      fill: false,
      order: 1,
    },
  ], {
    plugins:{ legend:{ position:'bottom' }, tooltip:{ mode:'index', intersect:false } },
    interaction:{ mode:'index', intersect:false },
    scales:{
      y:{ beginAtZero:true,
          ticks:{ callback:v => '₹' + (v >= 100000 ? (v/100000).toFixed(1)+'L' : (v >= 1000 ? (v/1000).toFixed(0)+'K' : v)) } },
    },
  });
  lineChart('chart-trend-roas', ts, [
    { label:'ROAS', data: ts.map(t => t.roas), borderColor:'#d97706', backgroundColor:'#d9770633', fill:true, tension:.25 },
  ]);
  barChart('chart-trend-orders', labels, [
    { label:'Orders', data: ts.map(t => t.orders), backgroundColor:'#1a3d7c' },
  ], { type: 'bar' });

  // Daily table
  document.querySelector('#tbl-daily tbody').innerHTML = ts.slice().reverse().map(t =>
    `<tr><td>${t.date}</td><td>${fmt.num(t.active_ads)}</td>` +
    `<td>${fmt.inr(t.spend)}</td><td>${fmt.inr(t.revenue)}</td><td>${fmt.num(t.orders)}</td>` +
    `<td>${fmt.roas(t.roas)}</td><td>${fmt.inr(t.cpm)}</td><td>${fmt.num1(t.ctr)}</td></tr>`
  ).join('') || '<tr><td colspan="8" class="empty">No data.</td></tr>';
}

// Stable palette for category lines — same color per category across charts.
const CAT_COLORS = [
  '#1a3d7c', '#059669', '#d97706', '#dc2626', '#7c3aed',
  '#0891b2', '#db2777', '#65a30d', '#ea580c', '#475569',
  '#a16207', '#1e40af',
];
const catColor = (() => {
  const map = new Map();
  return name => {
    if (!map.has(name)) map.set(name, CAT_COLORS[map.size % CAT_COLORS.length]);
    return map.get(name);
  };
})();

// Build per-(category, date) buckets from the current filtered rows. Used
// for the two multi-line trend charts on the Categories page.
function categoryTimeSeries(rows) {
  const byCat = new Map();        // category -> Map(date -> {spend, revenue})
  const allDates = new Set();
  for (const r of rows) {
    if (!r.category) continue;
    if (!byCat.has(r.category)) byCat.set(r.category, new Map());
    const m = byCat.get(r.category);
    if (!m.has(r.date)) m.set(r.date, { spend: 0, revenue: 0 });
    const b = m.get(r.date);
    b.spend   += r.spend   || 0;
    b.revenue += r.revenue || 0;
    allDates.add(r.date);
  }
  const dates = [...allDates].sort();
  // Build datasets — dense (zero-fill missing dates so lines stay continuous)
  const buildDataset = (metric) => [...byCat.entries()]
    // Sort by total spend desc so legend matches table order
    .map(([cat, m]) => {
      const total = [...m.values()].reduce((s, v) => s + v.spend, 0);
      return { cat, m, total };
    })
    .sort((a, b) => b.total - a.total)
    .map(({ cat, m }) => ({
      label: cat,
      data: dates.map(d => {
        const v = m.get(d);
        if (!v) return 0;
        if (metric === 'spend')   return Math.round(v.spend);
        if (metric === 'roas')    return v.spend > 0 ? +(v.revenue / v.spend).toFixed(2) : 0;
        return 0;
      }),
      borderColor:     catColor(cat),
      backgroundColor: catColor(cat) + '22',
      tension: .25,
      fill: false,
      pointRadius: 2,
    }));
  return { dates, spend: buildDataset('spend'), roas: buildDataset('roas') };
}

// Daily Spend × Category matrix table — rows = dates (newest first), cols
// = categories. Last row = per-category total, last column = per-day total.
// Shows for any window size — single-day = one row (Today filter), 3D = 3
// rows, 7D = 7 rows, etc. Hidden only when there are literally zero dates
// in the filtered set.
function renderCatDailyTable(cts, _singleDay) {
  const card = document.getElementById('card-cat-daily-table');
  const tbl  = document.getElementById('tbl-cat-daily');
  if (!card || !tbl) return;
  if (cts.dates.length === 0) {
    card.style.display = 'none';
    return;
  }
  card.style.display = '';

  const cats = cts.spend.map(s => s.label);
  // Newest first — easier to scan recent days at the top
  const order = cts.dates.map((_, i) => i).sort((a, b) => cts.dates[b].localeCompare(cts.dates[a]));

  let html = '<thead><tr><th>Date</th>';
  cats.forEach(c => { html += `<th data-type="num">${c}</th>`; });
  html += '<th data-type="num"><strong>Day Total</strong></th></tr></thead><tbody>';

  const colTotals = cats.map(() => 0);
  let grandTotal = 0;
  for (const i of order) {
    const dayRow = cats.map((_, ci) => cts.spend[ci].data[i] || 0);
    const dayTotal = dayRow.reduce((a, b) => a + b, 0);
    grandTotal += dayTotal;
    dayRow.forEach((v, ci) => colTotals[ci] += v);
    html += `<tr><td><strong>${cts.dates[i]}</strong></td>`;
    dayRow.forEach((v, ci) => {
      // Color cells with > 5% of the day's spend so the eye lands on
      // dominant categories per day without scanning every number.
      const pct = dayTotal > 0 ? (v / dayTotal) : 0;
      const style = pct >= 0.30 ? 'background:#dbeafe;font-weight:600'
                   : pct >= 0.15 ? 'background:#eff6ff'
                   : '';
      html += `<td style="${style}">${v > 0 ? fmt.inr(v) : '<span class="subtle">—</span>'}</td>`;
    });
    html += `<td><strong>${fmt.inr(dayTotal)}</strong></td></tr>`;
  }
  // Footer: per-category totals
  html += '<tr style="background:#f8f9fc;border-top:2px solid #e5e7eb"><td><strong>Total</strong></td>';
  colTotals.forEach(v => { html += `<td><strong>${fmt.inr(v)}</strong></td>`; });
  html += `<td><strong>${fmt.inr(grandTotal)}</strong></td></tr>`;
  html += '</tbody>';
  tbl.innerHTML = html;
}

function renderDrillDown(rows) {
  // Show only when the operator is drilling into something specific:
  // a product, OR exactly one category chip, OR exactly one creative chip.
  // Otherwise the tables would just repeat the page-level totals.
  const card = document.getElementById('card-drilldown');
  if (!card) return;
  const prodFams  = [...F.product_families];
  const singleCat = F.categories.size === 1 ? [...F.categories][0] : null;
  const singleCt  = F.creative_types.size === 1 ? [...F.creative_types][0] : null;
  if (prodFams.length === 0 && !singleCat && !singleCt) {
    card.style.display = 'none';
    return;
  }
  card.style.display = '';

  const titleParts = [];
  if (prodFams.length) titleParts.push(`Product${prodFams.length>1?'s':''} = ${prodFams.join(', ')}`);
  if (singleCat) titleParts.push(`Category = ${singleCat}`);
  if (singleCt)  titleParts.push(`Creative = ${singleCt}`);
  document.getElementById('drill-title').textContent = titleParts.join(' · ');

  // Aggregate the filtered rows by creative_type and by adset_id.
  // Adset is the level where audience targeting lives in Meta, so the
  // structure table is grouped per-adset (campaigns appear in the first
  // column as "Campaign → Ad-set").
  const byCT = new Map();
  const byAdset = new Map();
  const allAds = new Set();
  let totalSpend = 0, totalRev = 0, totalPur = 0;
  for (const r of rows) {
    allAds.add(r.ad_id);
    totalSpend += r.spend || 0;
    totalRev   += r.revenue || 0;
    totalPur   += r.purchases || 0;

    const ct = r.creative_type || 'Other';
    if (!byCT.has(ct)) byCT.set(ct, { ads: new Set(), spend: 0, revenue: 0, purchases: 0 });
    const x = byCT.get(ct);
    x.ads.add(r.ad_id);
    x.spend     += r.spend     || 0;
    x.revenue   += r.revenue   || 0;
    x.purchases += r.purchases || 0;

    if (r.adset_id) {
      if (!byAdset.has(r.adset_id)) byAdset.set(r.adset_id, {
        campaign_name: r.campaign_name || '(unnamed)',
        adset_name:    r.adset_name    || '(no adset name)',
        portal:        r.portal        || '',
        ads: new Set(), spend: 0, revenue: 0, purchases: 0,
      });
      const y = byAdset.get(r.adset_id);
      y.ads.add(r.ad_id);
      y.spend     += r.spend     || 0;
      y.revenue   += r.revenue   || 0;
      y.purchases += r.purchases || 0;
    }
  }

  // Summary line above the two tables
  const realRoas = totalSpend > 0 ? totalRev / totalSpend : 0;
  document.getElementById('drill-summary').innerHTML =
    `<b>${allAds.size}</b> active ads · ` +
    `Spend <b>${fmt.inr(totalSpend)}</b> · ` +
    `Pixel Orders <b>${fmt.num(totalPur)}</b> · ` +
    `Pixel Revenue <b>${fmt.inr(totalRev)}</b> · ` +
    `Pixel ROAS <b>${fmt.roas(realRoas)}</b>` +
    `<div class="subtle" style="margin-top:4px">Note: orders / revenue / ROAS below are Meta-pixel-attributed (Shopify can't filter by product/creative).</div>`;

  // Creative-type breakdown
  const ctRows = [...byCT.entries()].map(([type, v]) => ({
    type, ads: v.ads.size, spend: v.spend, revenue: v.revenue, orders: v.purchases,
    roas: v.spend > 0 ? v.revenue / v.spend : 0,
  })).sort((a, b) => b.spend - a.spend);
  document.getElementById('drill-ct-tbody').innerHTML = ctRows.map(r =>
    `<tr><td><strong>${r.type}</strong></td>` +
    `<td>${fmt.num(r.ads)}</td>` +
    `<td>${fmt.inr(r.spend)}</td>` +
    `<td>${fmt.num(r.orders)}</td>` +
    `<td>${fmt.inr(r.revenue)}</td>` +
    `<td>${fmt.roas(r.roas)}</td></tr>`
  ).join('') || '<tr><td colspan="6" class="empty">No data for current filter.</td></tr>';

  // Adset breakdown (top 50 by spend) — joined with PAYLOAD.adsets so we
  // get audience inclusions / exclusions per adset.
  // Rolling ROAS columns (Live / 3D / 7D) need data outside the operator's
  // current date window, so we compute them from RAW filtered only by the
  // non-date filters that match the current drill-down slice.
  const adsetMeta = PAYLOAD.adsets || {};
  const today = PAYLOAD.until;
  const dayMs = 86400000;
  const d3 = new Date(today + 'T00:00:00'); d3.setDate(d3.getDate() - 2);   // 3D inclusive of today = today-2..today
  const d7 = new Date(today + 'T00:00:00'); d7.setDate(d7.getDate() - 6);   // 7D inclusive
  const d3Str = `${d3.getFullYear()}-${String(d3.getMonth()+1).padStart(2,'0')}-${String(d3.getDate()).padStart(2,'0')}`;
  const d7Str = `${d7.getFullYear()}-${String(d7.getMonth()+1).padStart(2,'0')}-${String(d7.getDate()).padStart(2,'0')}`;

  // Pre-aggregate rolling totals per adset_id from RAW (all dates),
  // applying the same non-date filters that produced the current rows[].
  const rolling = new Map();
  const passNonDate = (r) => {
    if (F.portals.size && !F.portals.has(r.portal)) return false;
    if (F.categories.size && !F.categories.has(r.category)) return false;
    if (F.creative_types.size && !F.creative_types.has(r.creative_type)) return false;
    if (F.product_families.size && !F.product_families.has(productFamily(r.product))) return false;
    return true;
  };
  for (const r of RAW) {
    if (!r.adset_id) continue;
    if (!passNonDate(r)) continue;
    if (!rolling.has(r.adset_id)) rolling.set(r.adset_id, {
      today_s:0, today_r:0, d3_s:0, d3_r:0, d7_s:0, d7_r:0
    });
    const x = rolling.get(r.adset_id);
    if (r.date === today)  { x.today_s += r.spend||0; x.today_r += r.revenue||0; }
    if (r.date >= d3Str)   { x.d3_s    += r.spend||0; x.d3_r    += r.revenue||0; }
    if (r.date >= d7Str)   { x.d7_s    += r.spend||0; x.d7_r    += r.revenue||0; }
  }
  const safeRoas = (rev, sp) => sp > 0 ? rev/sp : 0;

  const adsetRows = [...byAdset.entries()].map(([id, v]) => {
    const meta = adsetMeta[id] || {};
    const roll = rolling.get(id) || { today_s:0, today_r:0, d3_s:0, d3_r:0, d7_s:0, d7_r:0 };
    return {
      adset_id: id,
      campaign: v.campaign_name,
      adset: v.adset_name,
      portal: v.portal,
      incl: meta.audiences_incl || '',
      excl: meta.audiences_excl || '',
      ads: v.ads.size,
      spend: v.spend,
      revenue: v.revenue,
      orders: v.purchases,
      roas:       v.spend > 0 ? v.revenue / v.spend : 0,
      roas_today: safeRoas(roll.today_r, roll.today_s),
      roas_3d:    safeRoas(roll.d3_r,    roll.d3_s),
      roas_7d:    safeRoas(roll.d7_r,    roll.d7_s),
    };
  }).sort((a, b) => b.spend - a.spend).slice(0, 50);

  const audienceCell = (s) => {
    if (!s) return '<span class="subtle">—</span>';
    const escaped = s.replace(/"/g, '&quot;');
    return `<span class="cell-name" style="max-width:240px;display:inline-block;vertical-align:middle" title="${escaped}">${s}</span>`;
  };

  const roasOrDash = (n) => (n == null || !isFinite(n) || n <= 0) ? '<span class="subtle">—</span>' : fmt.roas(n);

  document.getElementById('drill-adset-tbody').innerHTML = adsetRows.map(r =>
    `<tr>` +
      `<td>` +
        `<span class="cell-name" style="max-width:340px;display:inline-block;vertical-align:middle" title="${(r.campaign+' / '+r.adset).replace(/"/g, '&quot;')}">` +
          `<strong>${r.campaign}</strong><br>` +
          `<span class="subtle">↳ ${r.adset}</span>` +
        `</span>` +
      `</td>` +
      `<td>${r.portal}</td>` +
      `<td>${audienceCell(r.incl)}</td>` +
      `<td>${audienceCell(r.excl)}</td>` +
      `<td>${fmt.num(r.ads)}</td>` +
      `<td>${fmt.inr(r.spend)}</td>` +
      `<td>${fmt.num(r.orders)}</td>` +
      `<td>${fmt.roas(r.roas)}</td>` +
      `<td>${roasOrDash(r.roas_today)}</td>` +
      `<td>${roasOrDash(r.roas_3d)}</td>` +
      `<td>${roasOrDash(r.roas_7d)}</td>` +
    `</tr>`
  ).join('') || '<tr><td colspan="11" class="empty">No data for current filter.</td></tr>';
}

function renderCategoriesPage(rows) {
  const cats = aggregate(rows, 'category').filter(c => c.key);
  for (const c of cats) {
    const subset = rows.filter(r => r.category === c.key);
    const d = successRateDetail(subset);
    c.success_rate = d.rate;
    c.success_n = d.success;
    c.success_total = d.total;
    c.category = c.key;
  }
  const onPage = document.getElementById('page-categories').classList.contains('active');
  if (onPage) {
    // Pie chart: BUDGET allocation by category (not spend). Slice size is
    // the sum of ACTIVE campaign daily-budgets whose ads are dominantly
    // in that category. Tooltip also surfaces actual spend + ROAS so the
    // operator can spot "budget allocated but not spent" or "spending
    // more than allocated".
    const campMeta = PAYLOAD.campaigns || {};
    // For each campaign that has spend in the window, decide its dominant
    // category (the category bucket with the most ad-day spend inside the
    // campaign). Use UNFILTERED rows so a campaign that mixes categories
    // still gets a single dominant bucket.
    const campCat = new Map();   // campaign_id -> dominant category
    const campCatSpend = new Map(); // campaign_id -> { cat: spend, ... }
    for (const r of RAW) {
      if (!r.campaign_id || !r.category) continue;
      if (!campCatSpend.has(r.campaign_id)) campCatSpend.set(r.campaign_id, {});
      const m = campCatSpend.get(r.campaign_id);
      m[r.category] = (m[r.category] || 0) + (r.spend || 0);
    }
    for (const [cid, m] of campCatSpend) {
      let bestCat = null, bestSpend = -1;
      for (const [k, v] of Object.entries(m)) if (v > bestSpend) { bestCat = k; bestSpend = v; }
      campCat.set(cid, bestCat);
    }
    // Now sum daily_budget per category (ACTIVE only).
    const budgetByCat = new Map();
    for (const [cid, meta] of Object.entries(campMeta)) {
      if (meta.status !== 'ACTIVE') continue;
      const cat = campCat.get(cid);
      if (!cat) continue;
      const b = (meta.daily_budget || 0);
      if (b <= 0) continue;
      budgetByCat.set(cat, (budgetByCat.get(cat) || 0) + b);
    }
    // Build pie data sorted by budget desc. Cross-reference cats for spend/ROAS.
    const spendByCat = new Map();
    cats.forEach(c => spendByCat.set(c.key, c));
    const pieRows = [...budgetByCat.entries()]
      .map(([cat, budget]) => {
        const c = spendByCat.get(cat) || { spend: 0, roas: 0 };
        return { cat, budget, spend: c.spend, roas: c.roas };
      })
      .filter(r => r.budget > 0)
      .sort((a, b) => b.budget - a.budget);
    const totalBudget = pieRows.reduce((s, r) => s + r.budget, 0);
    const pieEmpty = pieRows.length === 0;
    document.getElementById('chart-cat-pie').style.display = pieEmpty ? 'none' : '';
    document.getElementById('cat-pie-empty').style.display = pieEmpty ? 'block' : 'none';
    if (!pieEmpty) {
      pieChart('chart-cat-pie',
        pieRows.map(r => r.cat),
        [{
          label: 'Daily budget',
          data: pieRows.map(r => Math.round(r.budget)),
          backgroundColor: pieRows.map(r => catColor(r.cat)),
          borderColor: '#fff',
          borderWidth: 2,
        }],
        {
          plugins: {
            legend: { position: 'right', labels: { boxWidth: 14, font: { size: 12 } } },
            tooltip: {
              callbacks: {
                label: (ctx) => {
                  const r = pieRows[ctx.dataIndex];
                  const pct = totalBudget > 0 ? (r.budget / totalBudget * 100).toFixed(1) : '0';
                  const spendPct = r.budget > 0 ? (r.spend / r.budget * 100).toFixed(0) : '–';
                  return [
                    `${r.cat}`,
                    `Daily budget: ₹${Math.round(r.budget).toLocaleString('en-IN')} (${pct}% of total)`,
                    `Spent (window): ₹${Math.round(r.spend).toLocaleString('en-IN')} · ROAS ${r.roas.toFixed(2)}x`,
                    `Utilization: ${spendPct}% of allocated daily × days`,
                  ];
                }
              }
            }
          }
        });
    }

    // Bar chart: spend + ROAS aggregated over the window (existing view)
    barChart('chart-cat-bar',
      cats.map(c => c.category),
      [
        { label:'Spend (₹)', data: cats.map(c => Math.round(c.spend)), backgroundColor:cats.map(c => catColor(c.category)), yAxisID:'y' },
        { label:'ROAS',      data: cats.map(c => +c.roas.toFixed(2)),  backgroundColor:'#059669', type:'line', yAxisID:'y1', tension:.25 },
      ],
      { type:'bar', scales:{
        y:{ beginAtZero:true, position:'left', ticks:{ callback:v => '₹' + (v >= 100000 ? (v/100000).toFixed(1)+'L' : v) } },
        y1:{ beginAtZero:true, position:'right', grid:{ drawOnChartArea:false } },
      } });

    // Daily trend — one line per category over the selected window.
    // Show on any filter (including Today, which renders as one point/
    // bar per category). Hints stay hidden — operator wants data visible
    // for every window size.
    const cts = categoryTimeSeries(rows);
    const noData = cts.dates.length === 0;
    const toggle = (canvasId, hintId, hide) => {
      const c = document.getElementById(canvasId);
      const h = document.getElementById(hintId);
      if (c) c.style.display = hide ? 'none' : '';
      if (h) h.style.display = hide ? 'block' : 'none';
    };
    toggle('chart-cat-trend-spend', 'cat-trend-spend-hint', noData);
    toggle('chart-cat-stack-spend', 'cat-stack-spend-hint', noData);
    toggle('chart-cat-trend-roas',  'cat-trend-roas-hint',  noData);
    if (!noData) {
      const tsForLineChart = cts.dates.map(d => ({ date: d }));
      lineChart('chart-cat-trend-spend', tsForLineChart, cts.spend, {
        plugins:{ legend:{ position:'bottom' } },
        scales:{ y:{ beginAtZero:true, ticks:{ callback:v => '₹' + (v >= 100000 ? (v/100000).toFixed(1)+'L' : (v >= 1000 ? (v/1000).toFixed(0)+'K' : v)) } } },
        interaction:{ mode:'index', intersect:false },
      });
      // Stacked-bar version of the same data — each bar is one day,
      // colored segments show how much of that day went to each category.
      // Better than the line chart for spotting day-over-day shifts in
      // budget mix; complementary, not a replacement.
      const stackDatasets = cts.spend.map(s => ({
        label: s.label,
        data: s.data,
        backgroundColor: s.borderColor || catColor(s.label),
        stack: 'spend',
      }));
      barChart('chart-cat-stack-spend', cts.dates, stackDatasets, {
        type: 'bar',
        plugins: { legend: { position: 'bottom' }, tooltip: { mode: 'index' } },
        scales: {
          x: { stacked: true },
          y: { stacked: true, beginAtZero: true,
               ticks: { callback: v => '₹' + (v >= 100000 ? (v/100000).toFixed(1)+'L' : (v >= 1000 ? (v/1000).toFixed(0)+'K' : v)) } },
        },
        interaction: { mode: 'index', intersect: false },
      });
      lineChart('chart-cat-trend-roas', tsForLineChart, cts.roas, {
        plugins:{ legend:{ position:'bottom' } },
        scales:{ y:{ beginAtZero:true, ticks:{ callback:v => v.toFixed(1) + 'x' } } },
        interaction:{ mode:'index', intersect:false },
      });
    }

    // Daily Spend × Category table. Rows = dates (newest first), cols =
    // categories. Last row is a per-category total, last col is per-day
    // total. Shows on any window now (single-day = one row).
    renderCatDailyTable(cts, false);

    // Drill-down: visible only when operator picks a Product or a single Category.
    // Otherwise we'd be summing across all categories and the per-creative /
    // per-campaign tables get unmanageable.
    renderDrillDown(rows);
  }
  const sorted = sortRows(cats, 'tbl-categories');
  applySortHeaders('tbl-categories');
  document.querySelector('#tbl-categories tbody').innerHTML = sorted.map(c =>
    `<tr><td><strong>${c.category}</strong></td>` +
    `<td>${fmt.num(c.active_ads)}</td><td>${fmt.num(c.active_camps)}</td>` +
    `<td>${fmt.inr(c.spend)}</td><td>${fmt.inr(c.revenue)}</td><td>${fmt.num(c.purchases)}</td>` +
    `<td>${fmt.roas(c.roas)}</td><td>${fmt.inr(c.cpm)}</td><td>${fmt.num1(c.ctr)}</td>` +
    `<td>${fmt.num1(c.atc_rate)}</td>` +
    `<td title="${c.success_n} of ${c.success_total} ads ROAS ≥ 1.0x">${c.success_rate == null ? '<span class="subtle">—</span>' : fmt.pct(c.success_rate) + `<span class="subtle"> (${c.success_n}/${c.success_total})</span>`}</td></tr>`
  ).join('') || '<tr><td colspan="11" class="empty">No data.</td></tr>';
}

function renderCreativesPage(rows) {
  const cts = aggregate(rows, 'creative_type').filter(c => c.key);
  for (const c of cts) {
    const subset = rows.filter(r => r.creative_type === c.key);
    const d = successRateDetail(subset);
    c.success_rate = d.rate;
    c.success_n = d.success;
    c.success_total = d.total;
    c.creative_type = c.key;
  }
  if (document.getElementById('page-creatives').classList.contains('active')) {
    barChart('chart-ct-bar',
      cts.map(c => c.creative_type),
      [
        { label:'Spend (₹)', data: cts.map(c => Math.round(c.spend)), backgroundColor:'#1a3d7c', yAxisID:'y' },
        { label:'ROAS',      data: cts.map(c => +c.roas.toFixed(2)),  backgroundColor:'#059669', type:'line', yAxisID:'y1', tension:.25 },
      ],
      { type:'bar', scales:{
        y:{ beginAtZero:true, position:'left', ticks:{ callback:v => '₹' + (v >= 100000 ? (v/100000).toFixed(1)+'L' : v) } },
        y1:{ beginAtZero:true, position:'right', grid:{ drawOnChartArea:false } },
      } });
  }
  const sorted = sortRows(cts, 'tbl-creatives');
  applySortHeaders('tbl-creatives');
  document.querySelector('#tbl-creatives tbody').innerHTML = sorted.map(c =>
    `<tr><td><strong>${c.creative_type}</strong></td>` +
    `<td>${fmt.num(c.active_ads)}</td><td>${fmt.inr(c.spend)}</td><td>${fmt.inr(c.revenue)}</td>` +
    `<td>${fmt.num(c.purchases)}</td><td>${fmt.roas(c.roas)}</td>` +
    `<td title="${c.success_n} of ${c.success_total} ads ROAS ≥ 1.0x">${c.success_rate == null ? '<span class="subtle">—</span>' : fmt.pct(c.success_rate) + `<span class="subtle"> (${c.success_n}/${c.success_total})</span>`}</td></tr>`
  ).join('') || '<tr><td colspan="7" class="empty">No data.</td></tr>';
}

// Sentiment code → short label for display. Operator wants both the
// code AND the name together: "st1 — Style/Design + Quality + Crystal
// Energy". Keep in sync with SENTIMENT_SEED in seed_lookups.py.
const SENTIMENT_LABELS = {
  // ST1 — Style / Design / Quality / Crystal Energy (6 permutations)
  st1a: 'Style + Design + Quality + Crystal Energy',
  st1b: 'Style + Crystal Energy + Design + Quality',
  st1c: 'Crystal Energy + Style + Design + Quality',
  st1d: 'Design + Quality + Style + Crystal Energy',
  st1e: 'Quality + Design + Style + Crystal Energy',
  st1f: 'Crystal Energy + Quality + Design + Style',
  // ST2 — Unisex Product / Quality / Crystal Energy (6 permutations)
  st2a: 'Unisex Product + Quality + Crystal Energy',
  st2b: 'Unisex Product + Crystal Energy + Quality',
  st2c: 'Quality + Unisex Product + Crystal Energy',
  st2d: 'Quality + Crystal Energy + Unisex Product',
  st2e: 'Crystal Energy + Unisex Product + Quality',
  st2f: 'Crystal Energy + Quality + Unisex Product',
  // ST3 — OG Gold Price Fear / Quality (2 orderings)
  st3a: 'OG Gold Price Fear + Quality',
  st3b: 'Quality + OG Gold Price Fear',
  // ST4 — Animal Storyline / Crystal Energy / Quality (6 permutations)
  st4a: 'Animal Storyline + Crystal Energy + Quality',
  st4b: 'Animal Storyline + Quality + Crystal Energy',
  st4c: 'Crystal Energy + Animal Storyline + Quality',
  st4d: 'Crystal Energy + Quality + Animal Storyline',
  st4e: 'Quality + Animal Storyline + Crystal Energy',
  st4f: 'Quality + Crystal Energy + Animal Storyline',
  // ST101 — Quality / Achievement / Testing
  st101a: 'Quality + Achievement + Testing',
  st101b: 'Quality + Testing + Achievement',
  st101c: 'Achievement + Quality + Testing',
  st101d: 'Achievement + Testing + Quality',
  st101e: 'Testing + Quality + Achievement',
  st101f: 'Testing + Achievement + Quality',
  // ST102 — Fear-Based Problem / SKU as Solution / Validation
  st102a: 'Fear Based Problem + SKU as Solution + Validation',
  st102b: 'Fear Based Problem + Validation + SKU as Solution',
  st102c: 'SKU as Solution + Fear Based Problem + Validation',
  st102d: 'SKU as Solution + Validation + Fear Based Problem',
  st102e: 'Validation + Fear Based Problem + SKU as Solution',
  st102f: 'Validation + SKU as Solution + Fear Based Problem',
  // ST103 — Desire / Fast Results / Quality
  st103a: 'Desire + Fast Results + Quality',
  st103b: 'Desire + Quality + Fast Results',
  st103c: 'Fast Results + Desire + Quality',
  st103d: 'Fast Results + Quality + Desire',
  st103e: 'Quality + Desire + Fast Results',
  st103f: 'Quality + Fast Results + Desire',
  // ST104 — Before/After / Testimonial / Testing
  st104a: 'Before/After + Testimonial + Testing',
  st104b: 'Before/After + Testing + Testimonial',
  st104c: 'Testimonial + Before/After + Testing',
  st104d: 'Testimonial + Testing + Before/After',
  st104e: 'Testing + Before/After + Testimonial',
  st104f: 'Testing + Testimonial + Before/After',
  // ST105 — Relevance / Validation / Trust
  st105a: 'Relevance + Validation + Trust',
  st105b: 'Relevance + Trust + Validation',
  st105c: 'Validation + Relevance + Trust',
  st105d: 'Validation + Trust + Relevance',
  st105e: 'Trust + Relevance + Validation',
  st105f: 'Trust + Validation + Relevance',
  // ST106 — Ingredient Story / Benefits / Quality
  st106a: 'Ingredient Story + Benefits + Quality',
  st106b: 'Ingredient Story + Quality + Benefits',
  st106c: 'Benefits + Ingredient Story + Quality',
  st106d: 'Benefits + Quality + Ingredient Story',
  st106e: 'Quality + Ingredient Story + Benefits',
  st106f: 'Quality + Benefits + Ingredient Story',
  // ST107 — Manufacturing/R&D / Quality / Achievements
  st107a: 'Manufacturing/R&D + Quality + Achievements',
  st107b: 'Manufacturing/R&D + Achievements + Quality',
  st107c: 'Quality + Manufacturing/R&D + Achievements',
  st107d: 'Quality + Achievements + Manufacturing/R&D',
  st107e: 'Achievements + Manufacturing/R&D + Quality',
  st107f: 'Achievements + Quality + Manufacturing/R&D',
  // ST108 — Offer / Deal
  st108a: 'Offer / Deal',
  // ST109 — Competitor Comparison / Achievements / Trust
  st109a: 'Competitor Comparison + Achievements + Trust',
  st109b: 'Competitor Comparison + Trust + Achievements',
  st109c: 'Achievements + Competitor Comparison + Trust',
  st109d: 'Achievements + Trust + Competitor Comparison',
  st109e: 'Trust + Competitor Comparison + Achievements',
  st109f: 'Trust + Achievements + Competitor Comparison',
  // ST110-112 — singletons
  st110a: 'Celebrity / Social Proof',
  st111a: 'Daily Routine',
  st112a: 'Launch Purpose + BTS + Usage',
};
function sentimentDisplay(code) {
  if (!code) return '(unset)';
  const label = SENTIMENT_LABELS[code.toLowerCase()];
  return label ? `${code} — ${label}` : code;
}

function renderSentimentsPage(rows) {
  // ── First pass: per-ad rollup so the summary uses real ad counts (not
  // ad-day rows). Tag each ad as classified or unclassified.
  // Display shows the code + label together (e.g. "st1 — Style/Design...").
  const adAgg = new Map();  // ad_id -> {sentiment_code, creative_type, spend, revenue}
  for (const r of rows) {
    if (!adAgg.has(r.ad_id)) adAgg.set(r.ad_id, {
      sentiment_code: r.sentiment_code || null,
      creative_type: r.creative_type || '?',
      spend: 0, revenue: 0,
    });
    const a = adAgg.get(r.ad_id);
    a.spend   += r.spend   || 0;
    a.revenue += r.revenue || 0;
  }

  // ── Summary stats — total ads, classified count, % classified, spend split
  let totalAds = 0, classifiedAds = 0;
  let totalSpend = 0, classifiedSpend = 0;
  for (const a of adAgg.values()) {
    totalAds++;
    totalSpend += a.spend;
    if (a.sentiment_code) {
      classifiedAds++;
      classifiedSpend += a.spend;
    }
  }
  const pctAds   = totalAds   ? (100 * classifiedAds   / totalAds  ) : 0;
  const pctSpend = totalSpend ? (100 * classifiedSpend / totalSpend) : 0;

  // ── Group rows by (sentiment_code, creative_type) for the detail table.
  // Track code and name separately — the operator wants two columns
  // ("Code" + "Name") instead of one combined "st1 — Style/Design…" cell.
  const groupMap = new Map();
  for (const a of adAgg.values()) {
    const code = a.sentiment_code || '(unset)';
    const name = a.sentiment_code
      ? (SENTIMENT_LABELS[a.sentiment_code.toLowerCase()] || '')
      : '';
    const key = code + '||' + a.creative_type;
    if (!groupMap.has(key)) groupMap.set(key, {
      sentiment_code: code,
      sentiment_name: name,
      sentiment_raw: a.sentiment_code,  // for sorting / styling
      creative_type: a.creative_type,
      active_ads: 0, spend: 0, revenue: 0,
    });
    const g = groupMap.get(key);
    g.active_ads++;
    g.spend   += a.spend;
    g.revenue += a.revenue;
  }
  const arr = [...groupMap.values()].map(g => ({
    ...g,
    roas: g.spend > 0 ? g.revenue / g.spend : 0,
  }));

  const sorted = sortRows(arr, 'tbl-sentiment');
  applySortHeaders('tbl-sentiment');

  // ── Render summary card (replaces the prior plain h3) + detail table
  const summaryHTML = `
    <div style="display:flex;gap:1rem;flex-wrap:wrap;margin-bottom:1rem;padding:1rem;background:#f8f9fc;border-radius:8px">
      <div style="flex:1;min-width:140px">
        <div style="font-size:.75rem;color:#6b7280;text-transform:uppercase;letter-spacing:.05em">Classified ads</div>
        <div style="font-size:1.5rem;font-weight:700;color:${pctAds >= 80 ? '#059669' : pctAds >= 30 ? '#d97706' : '#dc2626'}">${fmt.num(classifiedAds)} / ${fmt.num(totalAds)}</div>
        <div style="font-size:.85rem;color:#6b7280">${pctAds.toFixed(1)}% of ads</div>
      </div>
      <div style="flex:1;min-width:140px">
        <div style="font-size:.75rem;color:#6b7280;text-transform:uppercase;letter-spacing:.05em">Classified spend</div>
        <div style="font-size:1.5rem;font-weight:700;color:${pctSpend >= 80 ? '#059669' : pctSpend >= 30 ? '#d97706' : '#dc2626'}">${fmt.inr(classifiedSpend)}</div>
        <div style="font-size:.85rem;color:#6b7280">${pctSpend.toFixed(1)}% of ₹${fmt.inr(totalSpend).replace('₹','')}</div>
      </div>
      <div style="flex:2;min-width:240px;font-size:.85rem;color:#475569">
        ${pctAds < 30 ? '<strong style="color:#dc2626">⚠ Most ads are (unset)</strong> — run <code>scripts/v2/classify_ads_llm.py</code> on EC2 to tag them via Claude API.' : pctAds < 80 ? 'Run the LLM classifier again to tag the remaining (unset) rows.' : '<strong style="color:#059669">✓ Most ads classified.</strong>'}
      </div>
    </div>`;

  // Inject summary just above the table (idempotent — replace if present)
  const tbl = document.getElementById('tbl-sentiment');
  let summaryEl = document.getElementById('sentiment-summary');
  if (!summaryEl) {
    summaryEl = document.createElement('div');
    summaryEl.id = 'sentiment-summary';
    tbl.parentNode.insertBefore(summaryEl, tbl);
  }
  summaryEl.innerHTML = summaryHTML;

  document.querySelector('#tbl-sentiment tbody').innerHTML = sorted.map(s => {
    const isUnset = !s.sentiment_raw;
    const cellStyle = isUnset ? 'color:#9ca3af;font-style:italic' : '';
    const nameCell = s.sentiment_name
      ? s.sentiment_name
      : (isUnset ? '<span class="subtle">—</span>' : '<span class="subtle">(unmapped)</span>');
    return `<tr style="${isUnset ? 'background:#fafafa' : ''}">` +
      `<td style="${cellStyle}"><strong>${s.sentiment_code}</strong></td>` +
      `<td style="${cellStyle}">${nameCell}</td>` +
      `<td>${s.creative_type}</td>` +
      `<td>${fmt.num(s.active_ads)}</td><td>${fmt.inr(s.spend)}</td>` +
      `<td>${fmt.inr(s.revenue)}</td><td>${fmt.roas(s.roas)}</td></tr>`;
  }).join('') || '<tr><td colspan="7" class="empty">No data.</td></tr>';
}

function renderHeatmapPage(rows) {
  const cats = [...new Set(rows.map(r => r.category).filter(Boolean))].sort();
  const cts  = [...new Set(rows.map(r => r.creative_type).filter(Boolean))].sort();
  let html = '<thead><tr><th>Category ↓ / Creative Type →</th>';
  cts.forEach(ct => html += `<th>${ct}</th>`);
  html += '<th>TOTAL</th></tr></thead><tbody>';
  cats.forEach(cat => {
    html += `<tr><td><strong>${cat}</strong></td>`;
    let totSpend = 0, totRev = 0;
    cts.forEach(ct => {
      const subset = rows.filter(r => r.category === cat && r.creative_type === ct);
      const a = aggregate(subset)[0];
      if (!a) { html += '<td class="subtle">—</td>'; return; }
      totSpend += a.spend; totRev += a.revenue;
      const cls = a.roas >= 2.5 ? 'rg' : a.roas >= 1.5 ? 'ro' : 'rr';
      html += `<td><span class="${cls}">${a.roas.toFixed(2)}x</span><br><span class="subtle">${fmt.inr(a.spend)}</span></td>`;
    });
    const rowRoas = totSpend > 0 ? (totRev / totSpend) : 0;
    const cls = rowRoas >= 2.5 ? 'rg' : rowRoas >= 1.5 ? 'ro' : 'rr';
    html += `<td><span class="${cls}"><strong>${rowRoas.toFixed(2)}x</strong></span><br><span class="subtle">${fmt.inr(totSpend)}</span></td>`;
    html += '</tr>';
  });
  html += '</tbody>';
  document.getElementById('tbl-heatmap').innerHTML = html;
}

// Aaj ki nayi ads — campaigns that started today (IST), category-wise budget.
// Reads PAYLOAD.new_today (precomputed server-side, independent of filters),
// so it's rendered once on load rather than from the filtered ad-day rows.
function renderNewToday() {
  const data = PAYLOAD.new_today || { dates: [], default_date: '', today: '', camps: [] };
  const dates = (data.dates && data.dates.length) ? data.dates : [data.today].filter(Boolean);
  const sel = document.getElementById('newtoday-date');
  if (sel && !sel.dataset.filled) {
    sel.innerHTML = dates.map(d =>
      `<option value="${d}">${d === data.today ? d + ' (today)' : d}</option>`).join('');
    sel.value = data.default_date || dates[0] || '';
    sel.dataset.filled = '1';
    sel.addEventListener('change', () => renderNewTodayFor(sel.value));
  }
  renderNewTodayFor(sel ? sel.value : (data.default_date || ''));
}

// Render the new-ads card for one selected date.
function renderNewTodayFor(date) {
  const data = PAYLOAD.new_today || { camps: [], today: '' };
  const camps = (data.camps || []).filter(c => c.date === date);
  const totalSpent  = camps.reduce((s, c) => s + (c.spent_today || 0), 0);
  const totalPushed = camps.reduce((s, c) => s + (c.budget_val || 0), 0);

  const meta = document.getElementById('newtoday-meta');
  if (meta) meta.textContent =
    `${date}${date === data.today ? ' (today)' : ''} · ${camps.length} nayi ads · ` +
    `${fmt.inr(totalPushed)}/day pushed · ${fmt.inr(totalSpent)} spent`;

  // Group by category
  const byCat = {};
  camps.forEach(c => {
    const v = (byCat[c.category] = byCat[c.category] || { n: 0, spent: 0, pushed: 0 });
    v.n++; v.spent += (c.spent_today || 0); v.pushed += (c.budget_val || 0);
  });
  const catEntries = Object.entries(byCat).sort((a, b) => b[1].pushed - a[1].pushed);

  // KPI cards
  const kpis = document.getElementById('newtoday-kpis');
  if (kpis) kpis.innerHTML =
    `<div class="kpi-card"><div class="kpi-lbl">Nayi Ads</div><div class="kpi-val">${camps.length}</div><div class="kpi-sub">${date}</div></div>` +
    `<div class="kpi-card"><div class="kpi-lbl">Total Pushed Budget /day</div><div class="kpi-val">${fmt.inr(totalPushed)}</div><div class="kpi-sub">${catEntries.length} categories</div></div>` +
    `<div class="kpi-card"><div class="kpi-lbl">Spent (that day)</div><div class="kpi-val">${fmt.inr(totalSpent)}</div><div class="kpi-sub">${totalPushed ? Math.round(totalSpent / totalPushed * 100) : 0}% of pushed</div></div>`;

  // Rollup table
  const rollup = document.querySelector('#tbl-newtoday-rollup tbody');
  if (rollup) {
    rollup.innerHTML = camps.length
      ? catEntries.map(([cat, v]) =>
          `<tr><td><strong>${cat}</strong></td><td>${v.n}</td><td>${fmt.inr(v.pushed)}</td><td>${fmt.inr(v.spent)}</td><td>${totalPushed ? (v.pushed / totalPushed * 100).toFixed(1) : '0.0'}%</td></tr>`
        ).join('') +
        `<tr style="background:#f8faff;font-weight:800"><td>TOTAL</td><td>${camps.length}</td><td>${fmt.inr(totalPushed)}</td><td>${fmt.inr(totalSpent)}</td><td>100.0%</td></tr>`
      : `<tr><td colspan="5" class="empty">${date === data.today ? 'Aaj abhi tak koi nayi ad push nahi hui — raat ko push karte hi yahan aa jayegi.' : 'Is din koi nayi ad nahi.'}</td></tr>`;
  }

  // Detail table
  const detail = document.querySelector('#tbl-newtoday-detail tbody');
  if (detail) {
    detail.innerHTML = camps.length
      ? camps.map(c =>
          `<tr><td>${c.category}</td><td>${c.product}</td>` +
          `<td class="cell-name">${campCell(c)}</td>` +
          `<td>${tag(c.portal)}</td><td>${pushedCell(c)}</td><td>${fmt.inr(c.spent_today)}</td>` +
          `<td>${scaleEstimate(c)}</td></tr>`
        ).join('')
      : '<tr><td colspan="7" class="empty">—</td></tr>';
  }

  renderNewTodayChart(camps, catEntries);
}

function renderNewTodayChart(camps, catEntries) {
  const wrap = document.getElementById('newtoday-chart-wrap');
  if (!wrap) return;
  if (!camps.length) { destroyChart('chart-newtoday'); wrap.style.display = 'none'; return; }
  wrap.style.display = '';
  const cats = catEntries.map(([cat]) => cat);          // budget desc (same order)
  const buckets = [
    { key: 'success',   label: '✅ Likely Success', color: '#0b8043' },
    { key: 'uncertain', label: '🟡 50-50',          color: '#e0a000' },
    { key: 'fail',      label: '❌ Likely Fail',     color: '#c5221f' },
    { key: 'lowdata',   label: '❔ Low data',        color: '#9ca3af' },
  ];
  const sums = {};                                      // cat -> kind -> spent
  cats.forEach(c => { sums[c] = {}; });
  camps.forEach(c => {
    const k = c.est_kind || 'lowdata';
    sums[c.category][k] = (sums[c.category][k] || 0) + (c.spent_today || 0);
  });
  const datasets = buckets.map(b => ({
    label: b.label,
    data: cats.map(c => Math.round(sums[c][b.key] || 0)),
    backgroundColor: b.color,
    borderWidth: 0,
  }));
  // Grow height with the category count so bars stay readable.
  wrap.style.height = Math.max(180, cats.length * 34 + 60) + 'px';
  barChart('chart-newtoday', cats, datasets, {
    indexAxis: 'y',
    animation: false,
    scales: {
      x: { stacked: true, ticks: { callback: v => '₹' + Number(v).toLocaleString('en-IN') } },
      y: { stacked: true },
    },
    plugins: {
      legend: { position: 'top', labels: { boxWidth: 12, font: { size: 11 } } },
      tooltip: { callbacks: { label: ctx =>
        `${ctx.dataset.label}: ₹${Number(ctx.parsed.x).toLocaleString('en-IN')}` } },
    },
  });
  // If the chart was built before the just-shown page finished laying out, the
  // canvas can come up under-sized — re-measure shortly after (setTimeout
  // fires reliably; rAF can be throttled when the tab isn't painting).
  setTimeout(() => { const c = charts['chart-newtoday']; if (c) c.resize(); }, 120);
}

// Success estimate cell: per-ad call — will this new ad likely SUCCEED or
// FAIL — from the historical SPEND-WEIGHTED ROAS of its product/category (how
// profitably past ads of this kind performed, bigger spend weighted more). A
// base-rate, not a guarantee; the ROAS, spend and sample are in the tooltip.
function scaleEstimate(c) {
  if (c.est_kind === 'lowdata' || c.est_roas == null)
    return `<span class="subtle" title="Not enough past ad spend to estimate">❔ low data</span>`;
  const basis = c.est_basis === 'product' ? 'product' : 'category';
  // ROAS drives the call but is kept off-screen — only the verdict shows. The
  // number stays in the hover tooltip for anyone who wants to verify.
  const tip = `Based on past ${basis} ads' spend-weighted ROAS (${c.est_roas}x)`;
  if (c.est_kind === 'success')
    return `<span class="delta-up" title="${tip}">✅ Likely Success</span>`;
  if (c.est_kind === 'fail')
    return `<span class="delta-down" title="${tip}">❌ Likely Fail</span>`;
  return `<span style="color:#a35a00;font-weight:700" title="${tip}">🟡 50-50</span>`;
}

// Pushed-budget cell — the budget the operator set, shown WITH its type so
// daily and lifetime are never conflated. ad-set = budget set on the ad sets
// (not stored at campaign level in the DB).
function pushedCell(c) {
  if (!c.budget_val || c.budget_type === 'adset')
    return `<span class="subtle">ad-set</span>`;
  const note = c.budget_note ? ` title="${c.budget_note}"` : '';
  return c.budget_type === 'daily'
    ? `<span${note}>${fmt.inr(c.budget_val)}<span class="subtle">/day</span></span>`
    : `${fmt.inr(c.budget_val)}<span class="subtle"> total</span>`;
}

// Campaign cell — ▶ opens the ad's video in a new tab. Prefers the direct mp4
// source (plays straight in the browser, works for dark/unpublished ad posts);
// falls back to the Facebook permalink. ▶ is prefixed so it stays visible even
// when the long name truncates.
function campCell(c) {
  const nm = c.name || '';
  const t = nm.replace(/"/g, '&quot;');
  const href = c.video_src || c.video_url;
  if (href)
    return `<a href="${href}" target="_blank" rel="noopener" title="▶ Watch video — ${t}"><span style="color:#1877f2;font-weight:700">▶</span> ${nm}</a>`;
  return `<span title="${t}">${nm}</span>`;
}

// Budget change cell for a new-today campaign: ↑/↓ vs the budget when it was
// first seen today, 🆕 on its first build, = when unchanged since launch.
function newTodayChange(c) {
  const amt = Math.abs(c.change_amt || 0);
  if (c.change_kind === 'up')
    return `<span class="delta-up">↑ +${Math.round(amt).toLocaleString('en-IN')}</span>`;
  if (c.change_kind === 'down')
    return `<span class="delta-down">↓ -${Math.round(amt).toLocaleString('en-IN')}</span>`;
  if (c.change_kind === 'same')
    return `<span class="subtle">=</span>`;
  return `<span style="color:#e37400;font-weight:700">🆕 NEW</span>`;
}

// Product Budget page: roll up daily_budget per product (via dominant
// product label per campaign) and per campaign. Shows current day's
// utilization + rolling ROAS so the operator can see where the budget
// is allocated and whether each bucket is earning its keep.
function renderProdBudgetPage(rows) {
  const campMeta = PAYLOAD.campaigns || {};

  // 1) Determine each campaign's dominant product label from RAW
  //    (full payload — so utilization calcs aren't biased by the
  //    operator's current date filter).
  const campSpendByProd = new Map();   // campaign_id -> { prod -> spend }
  for (const r of RAW) {
    if (!r.campaign_id) continue;
    const p = r.product || '(no product tag)';
    if (!campSpendByProd.has(r.campaign_id)) campSpendByProd.set(r.campaign_id, {});
    const m = campSpendByProd.get(r.campaign_id);
    m[p] = (m[p] || 0) + (r.spend || 0);
  }
  const campToProduct = new Map();
  for (const [cid, m] of campSpendByProd) {
    let bestProd = '(no product tag)', bestSp = -1;
    for (const [k, v] of Object.entries(m)) if (v > bestSp) { bestProd = k; bestSp = v; }
    campToProduct.set(cid, bestProd);
  }

  // 2) Compute per-campaign rolling spend + revenue from RAW
  const today = PAYLOAD.until;
  const d3 = new Date(today + 'T00:00:00'); d3.setDate(d3.getDate() - 2);
  const d7 = new Date(today + 'T00:00:00'); d7.setDate(d7.getDate() - 6);
  const fmtDate = d => `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;
  const d3Str = fmtDate(d3), d7Str = fmtDate(d7);

  const campStats = new Map();   // campaign_id -> { spend_today, rev_today, spend_3d, rev_3d, spend_7d, rev_7d, spend_window, rev_window }
  const inWindow = (r) => r.date >= F.fromDate && r.date <= F.toDate;
  for (const r of RAW) {
    if (!r.campaign_id) continue;
    if (!campStats.has(r.campaign_id)) campStats.set(r.campaign_id, {
      spend_today:0, rev_today:0, spend_3d:0, rev_3d:0,
      spend_7d:0, rev_7d:0, spend_window:0, rev_window:0,
    });
    const x = campStats.get(r.campaign_id);
    const sp = r.spend || 0, rv = r.revenue || 0;
    if (r.date === today)  { x.spend_today += sp; x.rev_today += rv; }
    if (r.date >= d3Str)   { x.spend_3d    += sp; x.rev_3d    += rv; }
    if (r.date >= d7Str)   { x.spend_7d    += sp; x.rev_7d    += rv; }
    if (inWindow(r))       { x.spend_window+= sp; x.rev_window+= rv; }
  }
  const safeRoas = (rev, sp) => sp > 0 ? rev/sp : 0;

  // 3) Build per-campaign rows (ACTIVE with daily_budget > 0 only)
  const campRows = [];
  for (const [cid, meta] of Object.entries(campMeta)) {
    if (meta.status !== 'ACTIVE') continue;
    const budget = meta.daily_budget || 0;
    if (budget <= 0) continue;
    const stats = campStats.get(cid) || {
      spend_today:0, rev_today:0, spend_3d:0, rev_3d:0,
      spend_7d:0, rev_7d:0, spend_window:0, rev_window:0,
    };
    campRows.push({
      campaign_id: cid,
      name: meta.name || '(unnamed)',
      product: campToProduct.get(cid) || '(no product tag)',
      portal: meta.portal || '',
      daily_budget: budget,
      spend_today: stats.spend_today,
      utilization: budget > 0 ? (stats.spend_today / budget * 100) : 0,
      spend_7d: stats.spend_7d,
      roas_today: safeRoas(stats.rev_today, stats.spend_today),
      roas_3d:    safeRoas(stats.rev_3d,    stats.spend_3d),
      roas_7d:    safeRoas(stats.rev_7d,    stats.spend_7d),
    });
  }

  // 4) KPI strip
  const totalBudget = campRows.reduce((s, r) => s + r.daily_budget, 0);
  const totalSpendToday = campRows.reduce((s, r) => s + r.spend_today, 0);
  const overallUtil = totalBudget > 0 ? (totalSpendToday / totalBudget * 100) : 0;
  const totalRev7d = campRows.reduce((s, r) => s + r.spend_7d * r.roas_7d, 0);
  const totalSp7d  = campRows.reduce((s, r) => s + r.spend_7d, 0);
  const blended7dRoas = totalSp7d > 0 ? totalRev7d / totalSp7d : 0;

  const kpis = [
    { l:'Active Campaigns',    v: fmt.num(campRows.length), s:'with daily budget configured' },
    { l:'Total Daily Budget',  v: fmt.inr(totalBudget),     s:'sum across active campaigns' },
    { l:'Spend Today',         v: fmt.inr(totalSpendToday), s:`${overallUtil.toFixed(0)}% of allocated` },
    { l:'7D Spend',            v: fmt.inr(totalSp7d),       s:'last 7 days' },
    { l:'7D ROAS (blended)',   v: fmt.roas(blended7dRoas),  s:'Meta-pixel-attributed', cls: blended7dRoas >= 2 ? 'good' : (blended7dRoas >= 1.5 ? 'warn' : 'bad') },
  ];
  document.getElementById('kpi-strip-prodbudget').innerHTML = kpis.map(c =>
    `<div class="kpi-card${c.cls ? ' kpi-' + c.cls : ''}"><div class="kpi-lbl">${c.l}</div>` +
    `<div class="kpi-val">${c.v}</div><div class="kpi-sub">${c.s}</div></div>`
  ).join('');

  // 5) Per-product rollup
  const prodMap = new Map();
  for (const r of campRows) {
    if (!prodMap.has(r.product)) prodMap.set(r.product, {
      product: r.product, camps:0, daily_budget:0, spend_today:0, spend_7d:0,
      rev_7d_acc: 0, spend_window:0, rev_window:0,
    });
    const x = prodMap.get(r.product);
    x.camps += 1;
    x.daily_budget += r.daily_budget;
    x.spend_today  += r.spend_today;
    x.spend_7d     += r.spend_7d;
    x.rev_7d_acc   += (r.spend_7d * r.roas_7d);     // recover revenue from roas*spend
    const s = campStats.get(r.campaign_id) || { spend_window:0, rev_window:0 };
    x.spend_window += s.spend_window;
    x.rev_window   += s.rev_window;
  }
  const prodRows = [...prodMap.values()].map(x => ({
    ...x,
    utilization: x.daily_budget > 0 ? (x.spend_today / x.daily_budget * 100) : 0,
    roas_7d:     x.spend_7d > 0     ? x.rev_7d_acc / x.spend_7d   : 0,
    roas_window: x.spend_window > 0 ? x.rev_window / x.spend_window : 0,
  }));

  // 6) Render — both tables sortable. Default sort: daily budget DESC.
  const sortedProds = sortRows(prodRows, 'tbl-prodbudget');
  applySortHeaders('tbl-prodbudget');
  document.querySelector('#tbl-prodbudget tbody').innerHTML = sortedProds.map(r =>
    `<tr><td><strong>${r.product}</strong></td>` +
    `<td>${fmt.num(r.camps)}</td>` +
    `<td>${fmt.inr(r.daily_budget)}</td>` +
    `<td>${fmt.inr(r.spend_today)}</td>` +
    `<td>${r.utilization.toFixed(0)}%</td>` +
    `<td>${fmt.inr(r.spend_7d)}</td>` +
    `<td>${fmt.roas(r.roas_7d)}</td>` +
    `<td>${fmt.roas(r.roas_window)}</td></tr>`
  ).join('') || '<tr><td colspan="8" class="empty">No active campaigns with daily budget.</td></tr>';

  const sortedCamps = sortRows(campRows, 'tbl-campbudget').slice(0, 100);
  applySortHeaders('tbl-campbudget');
  document.querySelector('#tbl-campbudget tbody').innerHTML = sortedCamps.map(r =>
    `<tr><td><span class="cell-name" style="max-width:340px;display:inline-block;vertical-align:middle" title="${(r.name||'').replace(/"/g, '&quot;')}">${r.name}</span></td>` +
    `<td>${r.product}</td>` +
    `<td>${r.portal}</td>` +
    `<td>${fmt.inr(r.daily_budget)}</td>` +
    `<td>${fmt.inr(r.spend_today)}</td>` +
    `<td>${r.utilization.toFixed(0)}%</td>` +
    `<td>${fmt.inr(r.spend_7d)}</td>` +
    `<td>${r.roas_today > 0 ? fmt.roas(r.roas_today) : '<span class="subtle">—</span>'}</td>` +
    `<td>${r.roas_3d    > 0 ? fmt.roas(r.roas_3d)    : '<span class="subtle">—</span>'}</td>` +
    `<td>${r.roas_7d    > 0 ? fmt.roas(r.roas_7d)    : '<span class="subtle">—</span>'}</td></tr>`
  ).join('') || '<tr><td colspan="10" class="empty">No active campaigns with daily budget.</td></tr>';
}

function renderProductsPage(rows) {
  const prods = aggregate(rows, 'product').filter(x => x.key);
  const adIdsByProd = new Map();
  const adFirstSeen = new Map();
  const adCategory  = new Map();
  for (const r of rows) {
    if (!adIdsByProd.has(r.product)) adIdsByProd.set(r.product, new Map());
    const m = adIdsByProd.get(r.product);
    if (!m.has(r.ad_id)) m.set(r.ad_id, { spend:0, revenue:0, purchases:0 });
    const a = m.get(r.ad_id);
    a.spend     += r.spend     || 0;
    a.revenue   += r.revenue   || 0;
    a.purchases += r.purchases || 0;
    adFirstSeen.set(r.ad_id, r.first_seen);
    adCategory.set(r.ad_id, r.category);
  }
  const ROASBKT = [1.5, 2.0, 2.5, 3.0, 4.0, 5.0];
  for (const p of prods) {
    const ads = adIdsByProd.get(p.key) || new Map();
    const launched = [];
    for (const [adId, m] of ads) {
      const fs = adFirstSeen.get(adId);
      if (fs && fs >= F.fromDate && fs <= F.toDate) {
        const adRoas = m.spend > 0 ? m.revenue / m.spend : 0;
        launched.push(adRoas);
      }
    }
    p.product = p.key;
    const adIds = [...(adIdsByProd.get(p.key) || new Map()).keys()];
    p.category = adIds.length ? (adCategory.get(adIds[0]) || '') : '';
    p.orders = p.purchases;
    ROASBKT.forEach(thr => {
      const key = 'hit_' + String(thr).replace('.', '').padEnd(2, '0').slice(0, 2);
      const hit = launched.filter(r => r >= thr).length;
      p[key] = launched.length > 0 ? Math.round(100 * hit / launched.length * 10) / 10 : null;
      p[key + '_n'] = launched.length;
      p[key + '_h'] = hit;
    });
  }
  const sorted = sortRows(prods, 'tbl-products');
  applySortHeaders('tbl-products');
  function rateCell(r, thr) {
    const key = 'hit_' + String(thr).replace('.', '').padEnd(2, '0').slice(0, 2);
    const v = r[key];
    if (v == null) return '<span class="subtle">—</span>';
    const cls = v >= 50 ? 'sr-hi' : v >= 25 ? 'sr-med' : v >= 10 ? 'sr-low' : 'sr-0';
    return `<span class="${cls}">${v.toFixed(0)}%</span><br><span class="subtle">${r[key + '_h']}/${r[key + '_n']}</span>`;
  }
  document.querySelector('#tbl-products tbody').innerHTML = sorted.map(p =>
    `<tr>
      <td><strong class="cell-name" title="${p.product}">${p.product}</strong></td>
      <td>${p.category || '<span class="subtle">—</span>'}</td>
      <td>${fmt.num(p.active_ads)}</td><td>${fmt.inr(p.spend)}</td>
      <td>${fmt.inr(p.revenue)}</td><td>${fmt.num(p.orders)}</td>
      <td>${fmt.roas(p.roas)}</td>
      <td>${rateCell(p, 1.5)}</td><td>${rateCell(p, 2.0)}</td><td>${rateCell(p, 2.5)}</td>
      <td>${rateCell(p, 3.0)}</td><td>${rateCell(p, 4.0)}</td><td>${rateCell(p, 5.0)}</td>
    </tr>`
  ).join('') || '<tr><td colspan="13" class="empty">No data.</td></tr>';
}

function renderProdSuccessPage(rows) {
  // Campaign-level success: for each product, group ad-days into campaigns,
  // compute campaign ROAS (over the selected period), then bucket campaigns
  // by ROAS thresholds.
  const ROASBKT = [1.5, 2.0, 2.5, 3.0, 4.0, 5.0];
  const MIN_CAMP_SPEND = 500;
  // product -> Map<campaign_id, {spend, revenue, purchases, name, category}>
  const byProd = new Map();
  for (const r of rows) {
    if (!r.product) continue;
    if (!byProd.has(r.product)) byProd.set(r.product, new Map());
    const camps = byProd.get(r.product);
    if (!camps.has(r.campaign_id)) camps.set(r.campaign_id, {
      spend:0, revenue:0, purchases:0,
      campaign_name:r.campaign_name, category:r.category,
    });
    const c = camps.get(r.campaign_id);
    c.spend     += r.spend     || 0;
    c.revenue   += r.revenue   || 0;
    c.purchases += r.purchases || 0;
  }
  const out = [];
  for (const [product, camps] of byProd) {
    const list = [...camps.values()].filter(c => c.spend >= MIN_CAMP_SPEND);
    if (list.length === 0) continue;
    let spend = 0, revenue = 0, orders = 0, bestRoas = 0;
    for (const c of list) {
      spend += c.spend; revenue += c.revenue; orders += c.purchases;
      const cr = c.spend > 0 ? c.revenue / c.spend : 0;
      if (cr > bestRoas) bestRoas = cr;
    }
    const row = {
      product, category: list[0].category || '',
      campaigns: list.length,
      spend, revenue, orders,
      roas: spend > 0 ? revenue / spend : 0,
      best_roas: bestRoas,
    };
    ROASBKT.forEach(thr => {
      const key = 'hit_' + String(thr).replace('.', '').padEnd(2, '0').slice(0, 2);
      const hit = list.filter(c => (c.spend > 0 ? c.revenue / c.spend : 0) >= thr).length;
      row[key] = list.length > 0 ? Math.round(100 * hit / list.length * 10) / 10 : null;
      row[key + '_n'] = list.length;
      row[key + '_h'] = hit;
    });
    out.push(row);
  }
  const sorted = sortRows(out, 'tbl-prodsuccess');
  applySortHeaders('tbl-prodsuccess');
  function rateCell(r, thr) {
    const key = 'hit_' + String(thr).replace('.', '').padEnd(2, '0').slice(0, 2);
    const v = r[key];
    if (v == null) return '<span class="subtle">—</span>';
    const cls = v >= 50 ? 'sr-hi' : v >= 25 ? 'sr-med' : v >= 10 ? 'sr-low' : 'sr-0';
    return `<span class="${cls}">${v.toFixed(0)}%</span><br><span class="subtle">${r[key + '_h']}/${r[key + '_n']}</span>`;
  }
  document.querySelector('#tbl-prodsuccess tbody').innerHTML = sorted.map(p =>
    `<tr>
      <td><strong class="cell-name" title="${p.product}">${p.product}</strong></td>
      <td>${p.category || '<span class="subtle">—</span>'}</td>
      <td><strong>${fmt.num(p.campaigns)}</strong></td>
      <td>${fmt.inr(p.spend)}</td>
      <td>${fmt.inr(p.revenue)}</td>
      <td>${fmt.num(p.orders)}</td>
      <td>${fmt.roas(p.roas)}</td>
      <td>${fmt.roas(p.best_roas)}</td>
      <td>${rateCell(p, 1.5)}</td><td>${rateCell(p, 2.0)}</td><td>${rateCell(p, 2.5)}</td>
      <td>${rateCell(p, 3.0)}</td><td>${rateCell(p, 4.0)}</td><td>${rateCell(p, 5.0)}</td>
    </tr>`
  ).join('') || '<tr><td colspan="14" class="empty">No products with campaigns ≥ ₹500 spend in selection.</td></tr>';
}

function renderAdsPage(rows, which) {
  const map = new Map();
  for (const r of rows) {
    if (!map.has(r.ad_id)) map.set(r.ad_id, {
      ad_id:r.ad_id, ad_name:r.ad_name, campaign_id:r.campaign_id,
      campaign_name:r.campaign_name, portal:r.portal, category:r.category,
      creative_type:r.creative_type, days_active:r.days_active,
      spend:0, revenue:0, orders:0,
    });
    const a = map.get(r.ad_id);
    a.spend   += r.spend   || 0;
    a.revenue += r.revenue || 0;
    a.orders  += r.purchases || 0;
  }
  const ads = [...map.values()].filter(a => a.spend >= 2000);
  for (const a of ads) a.roas = a.spend > 0 ? a.revenue / a.spend : 0;
  ads.sort((a, b) => which === 'top' ? b.roas - a.roas : a.roas - b.roas);
  const top50 = ads.slice(0, 50);
  const tableId = which === 'top' ? 'tbl-topads' : 'tbl-bottomads';
  const sorted = sortRows(top50, tableId);
  applySortHeaders(tableId);
  document.querySelector(`#${tableId} tbody`).innerHTML = sorted.map(a =>
    `<tr>
      <td>${tag(a.portal)}</td>
      <td>${a.category || '<span class="subtle">—</span>'}</td>
      <td>${a.creative_type || '<span class="subtle">—</span>'}</td>
      <td><div class="cell-name" title="${(a.ad_name||'').replace(/"/g,'&quot;')}"><strong>${a.ad_name||'?'}</strong><br><span class="subtle">${a.campaign_name||''}</span></div></td>
      <td>${a.days_active != null ? a.days_active + 'd' : '—'}</td>
      <td>${fmt.inr(a.spend)}</td><td>${fmt.inr(a.revenue)}</td>
      <td>${fmt.num(a.orders)}</td><td>${fmt.roas(a.roas)}</td>
    </tr>`
  ).join('') || `<tr><td colspan="9" class="empty">No ads with ≥ ₹2K spend in selection.</td></tr>`;
}

// ── CSV export ──────────────────────────────────────────────────────────
function exportCSV(which) {
  const rows = applyFilters(RAW);
  let csv = '';
  if (which === 'products') {
    const prods = aggregate(rows, 'product').filter(x => x.key);
    csv = 'Product,Active Ads,Spend,Revenue,Orders,ROAS\n' +
      prods.map(p => `"${p.key}",${p.active_ads},${Math.round(p.spend)},${Math.round(p.revenue)},${p.purchases},${p.roas.toFixed(2)}`).join('\n');
  } else if (which === 'catdaily') {
    // Daily Spend × Category — same matrix as the on-screen table
    const cts = categoryTimeSeries(rows);
    const cats = cts.spend.map(s => s.label);
    csv = 'Date,' + cats.map(c => `"${c}"`).join(',') + ',Total\n';
    cts.dates.forEach((d, i) => {
      const row = cats.map((_, ci) => Math.round(cts.spend[ci].data[i] || 0));
      const total = row.reduce((a, b) => a + b, 0);
      csv += `${d},${row.join(',')},${total}\n`;
    });
    // Footer: per-category totals
    const colTotals = cats.map((_, ci) => cts.spend[ci].data.reduce((a, b) => a + (b || 0), 0));
    csv += `Total,${colTotals.map(v => Math.round(v)).join(',')},${Math.round(colTotals.reduce((a,b)=>a+b,0))}\n`;
  } else if (which === 'prodsuccess') {
    const ROASBKT = [1.5, 2.0, 2.5, 3.0, 4.0, 5.0];
    const MIN = 500;
    const byProd = new Map();
    for (const r of rows) {
      if (!r.product) continue;
      if (!byProd.has(r.product)) byProd.set(r.product, new Map());
      const m = byProd.get(r.product);
      if (!m.has(r.campaign_id)) m.set(r.campaign_id, { spend:0, revenue:0, purchases:0, category:r.category });
      const c = m.get(r.campaign_id);
      c.spend += r.spend||0; c.revenue += r.revenue||0; c.purchases += r.purchases||0;
    }
    csv = 'Product,Category,CampaignsPublished,Spend,Revenue,Orders,AggROAS,BestCampROAS,' +
          ROASBKT.map(t => `Hit${t}x_pct,Hit${t}x_count,Hit${t}x_total`).join(',') + '\n';
    for (const [product, camps] of byProd) {
      const list = [...camps.values()].filter(c => c.spend >= MIN);
      if (!list.length) continue;
      let s=0,r=0,o=0,b=0;
      for (const c of list) { s+=c.spend; r+=c.revenue; o+=c.purchases; const cr=c.spend>0?c.revenue/c.spend:0; if (cr>b)b=cr; }
      const buckets = ROASBKT.map(t => {
        const h = list.filter(c => (c.spend>0?c.revenue/c.spend:0) >= t).length;
        return `${list.length>0?(100*h/list.length).toFixed(1):0},${h},${list.length}`;
      }).join(',');
      csv += `"${product}","${list[0].category||''}",${list.length},${Math.round(s)},${Math.round(r)},${o},${(s>0?r/s:0).toFixed(2)},${b.toFixed(2)},${buckets}\n`;
    }
  } else {
    csv = 'Portal,Category,Creative,Ad,Campaign,Spend,Revenue,Orders,ROAS,DaysActive\n';
    const map = new Map();
    for (const r of rows) {
      if (!map.has(r.ad_id)) map.set(r.ad_id, { ...r, _spend:0, _revenue:0, _orders:0 });
      const a = map.get(r.ad_id);
      a._spend += r.spend; a._revenue += r.revenue; a._orders += r.purchases || 0;
    }
    let ads = [...map.values()].filter(a => a._spend >= 1000);
    if (which === 'topads') ads.sort((a, b) => (b._revenue / Math.max(b._spend, 1)) - (a._revenue / Math.max(a._spend, 1)));
    if (which === 'bottomads') ads.sort((a, b) => (a._revenue / Math.max(a._spend, 1)) - (b._revenue / Math.max(b._spend, 1)));
    csv += ads.map(a => {
      const roas = a._spend > 0 ? (a._revenue / a._spend).toFixed(2) : '0';
      return `"${a.portal}","${a.category||''}","${a.creative_type||''}","${(a.ad_name||'').replace(/"/g, '""')}","${(a.campaign_name||'').replace(/"/g, '""')}",${Math.round(a._spend)},${Math.round(a._revenue)},${a._orders},${roas},${a.days_active||''}`;
    }).join('\n');
  }
  const blob = new Blob([csv], { type:'text/csv' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url; a.download = which + '_' + F.fromDate + '_' + F.toDate + '.csv';
  document.body.appendChild(a); a.click(); a.remove();
  URL.revokeObjectURL(url);
}

// ── Master apply (re-renders every page-relevant section) ───────────────
function apply() {
  const rows = applyFilters(RAW);
  const prevRows = getCompareSet();
  // Always render Overview KPI strip (visible on Overview page)
  renderOverview(rows, prevRows);
  // Render whichever page is active for charts/tables that need a visible canvas
  const activePage = document.querySelector('.page.active');
  const id = activePage ? activePage.id.replace('page-', '') : 'overview';
  if (id === 'trends')      renderTrends(rows);
  if (id === 'categories')  renderCategoriesPage(rows);
  if (id === 'creatives')   renderCreativesPage(rows);
  if (id === 'sentiments')  renderSentimentsPage(rows);
  if (id === 'heatmap')   { renderHeatmapPage(rows); renderNewToday(); }
  if (id === 'prodbudget')  renderProdBudgetPage(rows);
  if (id === 'products')    renderProductsPage(rows);
  if (id === 'prodsuccess') renderProdSuccessPage(rows);
  if (id === 'topads')      renderAdsPage(rows, 'top');
  if (id === 'bottomads')   renderAdsPage(rows, 'bottom');
  // Always pre-compute non-chart tables for unsorted state — cheap
  // (charts skip themselves when their canvas is hidden)
}

// ── Setup sorts + initial render ────────────────────────────────────────
setupSort('tbl-categories', 'spend');
setupSort('tbl-creatives',  'spend');
setupSort('tbl-prodbudget', 'daily_budget');
setupSort('tbl-campbudget', 'daily_budget');
setupSort('tbl-products',   'spend');
setupSort('tbl-prodsuccess','spend');
setupSort('tbl-topads',     'roas', 'desc');
setupSort('tbl-bottomads',  'roas', 'asc');
setupSort('tbl-sentiment',  'spend');

// Default to 7-day window so trends/charts are immediately useful.
// Operator can drop to "Today" or extend to 30D from the chip row.
(document.querySelector('.preset-btn[data-days="7"]') ||
 document.querySelector('.preset-btn[data-days="1"]')).click();
activatePageFromHash();   // heatmap card renders via apply() when that page is shown
</script>
</body>
</html>
"""
def main():
    p = argparse.ArgumentParser()
    p.add_argument('--days', type=int, default=30,
                   help='Pull last N days of data (default 30). Larger = more data, larger HTML.')
    p.add_argument('--since', help='YYYY-MM-DD (overrides --days)')
    p.add_argument('--until', help='YYYY-MM-DD (overrides --days)')
    p.add_argument('--out', default=str(OUT_DIR / 'v2' / 'categories.html'))
    p.add_argument('--db', help='SQLite path (default state/ntn.db)')
    args = p.parse_args()

    today = datetime.now(IST).date()
    end = datetime.strptime(args.until, '%Y-%m-%d').date() if args.until else today
    if args.since:
        start = datetime.strptime(args.since, '%Y-%m-%d').date()
    else:
        start = end - timedelta(days=args.days - 1)
    since = start.isoformat()
    until = end.isoformat()

    conn = db_connect(Path(args.db)) if args.db else db_connect()

    print(f"📊 Building analytical dashboard")
    print(f"   Period: {since} → {until}")

    rows = fetch_ad_days(conn, since, until)
    dimensions = fetch_dimensions(conn, since, until)
    freshness = fetch_freshness(conn)
    shopify_daily = fetch_shopify_daily(conn, since, until)
    adsets = fetch_adsets(conn, since, until)
    campaigns = fetch_active_campaign_budgets(conn, since, until)
    new_today = fetch_new_today(conn)
    active_snapshot = fetch_active_snapshot(conn)
    active_snapshot_daily = fetch_active_snapshot_daily(conn, days_back=30)

    payload = {
        'since': since,
        'until': until,
        'rows': rows,
        'shopify_daily': shopify_daily,    # Real orders + revenue per portal/date
        'adsets': adsets,                   # adset_id -> {name, audiences_incl, audiences_excl, ...}
        'campaigns': campaigns,             # campaign_id -> {name, daily_budget, status, portal}
        'new_today': new_today,             # {date, camps[]} — campaigns started today, category-wise
        'active_snapshot': active_snapshot, # live effective_status=ACTIVE counts straight from Meta API
        'active_snapshot_daily': active_snapshot_daily, # last-snapshot-of-day trend (last 30 days)
        'dimensions': dimensions,
        'freshness': freshness,
        'updated_at': now_iso(),
    }
    payload_json = json.dumps(payload, default=str, ensure_ascii=False, separators=(',', ':'))

    print(f"   {len(rows):,} ad-day rows")
    print(f"   Dimensions: {{p: {len(dimensions['portals'])}, c: {len(dimensions['categories'])}, ct: {len(dimensions['creative_types'])}, prod: {len(dimensions['products'])}}}")
    print(f"   Payload: {len(payload_json):,} chars")

    html = render_html(payload_json, since, until)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding='utf-8')
    print(f"\n✅ Wrote {out_path}  ({len(html):,} bytes)")
    conn.close()


if __name__ == '__main__':
    main()
