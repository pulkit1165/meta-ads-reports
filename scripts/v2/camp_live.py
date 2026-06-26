#!/usr/bin/env python3
"""
camp_live.py — shared live fetch of ALL active Meta campaigns + per-campaign
metrics, used by the hourly snapshot collector (camp_snapshot.py) and the
15-minute alert engine (camp_alerts.py).

Per-campaign numbers are Meta PIXEL-attributed (spend/revenue/orders/ROAS).
Shopify ground truth can't attribute at campaign level (~11% UTM), so pixel is
the only viable per-campaign source — every consumer must label it as such.

fetch_active_campaigns(token, account_ids=None) -> list[dict] with keys:
  account_id, account_name, campaign_id, campaign_name, objective,
  created_time, age_hours, daily_budget (₹), spend (₹), revenue (₹), roas,
  orders, impressions, clicks, ctr (%), cpc (₹), cpm (₹), cpa (₹)
"""
import json
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta

API = "https://graph.facebook.com/v19.0/"
IST = timezone(timedelta(hours=5, minutes=30))


def _get(url):
    with urllib.request.urlopen(url, timeout=60) as r:
        return json.load(r)


def _paged(path, params, token, cap=2000):
    params = dict(params)
    params['access_token'] = token
    url = API + path + '?' + urllib.parse.urlencode(params)
    out = []
    while url and len(out) < cap:
        d = _get(url)
        out += d.get('data', [])
        url = d.get('paging', {}).get('next')
    return out


def _action(rows, atype):
    """Return value of action_type (prefer omni_purchase superset; never sum
    omni_X + X — that double-counts)."""
    if not rows:
        return 0.0
    for x in rows:
        if x.get('action_type') == atype:
            return float(x.get('value') or 0)
    return 0.0


def list_accounts(token):
    return _paged('me/adaccounts', {'fields': 'account_id,name', 'limit': 500}, token)


def _batch_ids(ids, fields, token):
    """Fetch many objects by id via ?ids=; split on error (deleted ids)."""
    if not ids:
        return {}
    q = urllib.parse.urlencode({'ids': ','.join(ids), 'fields': fields, 'access_token': token})
    try:
        return _get(API + '?' + q)
    except Exception:
        if len(ids) == 1:
            return {}
        m = len(ids) // 2
        d = {}
        d.update(_batch_ids(ids[:m], fields, token))
        d.update(_batch_ids(ids[m:], fields, token))
        return d


def fetch_active_campaigns(token, account_ids=None, now=None):
    """account_ids: list of 'act_<id>' (default = all accessible accounts)."""
    now = now or datetime.now(IST)
    if account_ids is None:
        accts = {f"act_{a['account_id']}": a['name'] for a in list_accounts(token)}
    else:
        accts = {a: a for a in account_ids}  # names filled below if missing
    out = []
    for aid, aname in accts.items():
        # 1) active campaigns: budget / created / objective / status
        CFIELDS = 'name,daily_budget,lifetime_budget,created_time,objective,effective_status'
        try:
            camps = _paged(f"{aid}/campaigns",
                           {'effective_status': "['ACTIVE']", 'fields': CFIELDS, 'limit': 500}, token)
        except Exception:
            continue
        cmeta = {c['id']: c for c in camps}
        # 2) today insights at campaign level
        ins = {}
        try:
            for r in _paged(f"{aid}/insights",
                            {'level': 'campaign', 'date_preset': 'today',
                             'fields': 'campaign_id,spend,purchase_roas,actions,action_values,'
                                       'impressions,clicks,inline_link_clicks,ctr,cpc,cpm',
                             'limit': 500}, token):
                ins[r['campaign_id']] = r
        except Exception:
            pass
        # 2b) campaigns that DELIVERED today but are no longer active (paused today) —
        # pull their meta so the snapshot/tracker shows them with status=Paused.
        extra = [cid for cid in ins if cid not in cmeta]
        if extra:
            got = _batch_ids(extra, CFIELDS, token)
            for cid, c in got.items():
                if isinstance(c, dict) and c.get('name'):
                    cmeta[cid] = c
        if not cmeta:
            continue
        # 3) ABO budget fallback: campaigns with no campaign-level (daily or lifetime)
        # budget set it at the ADSET level. Fetch adsets PER-CAMPAIGN (only for the
        # delivering ones that lack a campaign budget) — an account-wide adset pull
        # gets truncated on big accounts and silently drops budgets, which would
        # leave a campaign at budget=0 and make the alert engine MISS it.
        need_adset = [cid for cid, c in cmeta.items()
                      if cid in ins
                      and not (int(c.get('daily_budget') or 0) or int(c.get('lifetime_budget') or 0))]
        adset_daily, adset_life = {}, {}
        for cid in need_adset:
            try:
                for a in _paged(f"{cid}/adsets",
                                {'effective_status': "['ACTIVE']",
                                 'fields': 'daily_budget,lifetime_budget', 'limit': 200}, token):
                    adset_daily[cid] = adset_daily.get(cid, 0) + int(a.get('daily_budget') or 0)
                    adset_life[cid] = adset_life.get(cid, 0) + int(a.get('lifetime_budget') or 0)
            except Exception:
                pass
        for cid, c in cmeta.items():
            r = ins.get(cid, {})
            spend = float(r.get('spend') or 0)
            revenue = _action(r.get('action_values'), 'omni_purchase')
            orders = int(_action(r.get('actions'), 'omni_purchase'))
            roas = (revenue / spend) if spend else 0.0
            # budget basis (paise): campaign daily → campaign lifetime → adset daily
            # → adset lifetime. Ensures spend% is always computable so no campaign
            # meeting the 30%/50% condition is skipped.
            db = (int(c.get('daily_budget') or 0) or int(c.get('lifetime_budget') or 0)
                  or adset_daily.get(cid, 0) or adset_life.get(cid, 0))
            ct = c.get('created_time', '')
            try:
                cdt = datetime.fromisoformat(ct.replace('+0000', '+00:00'))
                age_h = round((now - cdt.astimezone(IST)).total_seconds() / 3600, 1)
            except Exception:
                age_h = None
            impr = int(r.get('impressions') or 0)
            # Only track campaigns that actually delivered today. Drops idle
            # "ACTIVE" zombies (e.g. Credit Line 06, no impressions for days); any
            # campaign auto-reappears the moment it incurs impressions. A campaign
            # paused mid-day keeps showing all of today (today's impressions stay
            # >0) with its ROAS frozen at the hour it went off.
            if impr <= 0:
                continue
            clicks = int(r.get('clicks') or 0)
            # Status = Meta's live state. A campaign you've paused reads "Paused"
            # (with today's ROAS frozen at its last active hour) even though it
            # delivered earlier today and so still appears (impr>0 inclusion above).
            status = 'Active' if c.get('effective_status') == 'ACTIVE' else 'Paused'
            out.append({
                'account_id': aid, 'account_name': accts.get(aid, aid),
                'campaign_id': cid, 'campaign_name': c.get('name', ''),
                'objective': c.get('objective', ''), 'status': status,
                'created_time': ct, 'age_hours': age_h,
                'daily_budget': round(db / 100, 2),         # paise -> ₹
                'spend': round(spend, 2), 'revenue': round(revenue, 2),
                'roas': round(roas, 4), 'orders': orders,
                'impressions': impr, 'clicks': clicks,
                'ctr': round(float(r.get('ctr') or 0), 4),
                'cpc': round(float(r.get('cpc') or 0), 2),
                'cpm': round(float(r.get('cpm') or 0), 2),
                'cpa': round(spend / orders, 2) if orders else 0.0,
            })
    return out


if __name__ == '__main__':
    import os, sys
    tok = os.environ['META_ACCESS_TOKEN']
    ids = sys.argv[1:] or None
    rows = fetch_active_campaigns(tok, ids)
    rows.sort(key=lambda x: -x['spend'])
    print(f"active campaigns: {len(rows)}")
    print(f"{'ACCOUNT':16} {'SPEND':>8} {'ROAS':>5} {'budget':>7} {'age_h':>6} {'orders':>6} {'ctr':>5} {'cpc':>6} | campaign")
    for r in rows[:20]:
        print(f"{r['account_name'][:16]:16} {r['spend']:>8,.0f} {r['roas']:>5.2f} {r['daily_budget']:>7,.0f} "
              f"{str(r['age_hours']):>6} {r['orders']:>6} {r['ctr']:>5.2f} {r['cpc']:>6,.1f} | {r['campaign_name'][:34]}")
