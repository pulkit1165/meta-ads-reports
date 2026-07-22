#!/usr/bin/env python3
"""
paras_video_report.py — per-VIDEO Meta ads report for Paras creatives.

For every distinct video creative (Meta video_id) used by a Paras-tagged ad
across all configured accounts, from a start date to today, it reports:

  * how many times the video was deployed (distinct campaigns = "tries")
  * ROAS over the video's first 1 / 3 / 7 days of life
  * total spend / revenue / ROAS
  * how many days each campaign that ran the video survived

"Paras video" = an ad whose name contains 'paras' (the creator tag) AND that
carries a video creative. The unit is the video_id, so the same reel used in
ten campaigns is one row with tries=10.

"1/3/7-day ROAS" is days-since-launch, not an attribution window — Meta has no
3-day attribution setting, so this is the only coherent reading. Day 0 = the
video's earliest ad created_time; the window sums every ad-day of that video
inside [launch, launch+N).

Campaign survival = calendar days from the campaign's start_time to its
stop_time, or to today if still running.

Usage:
  META_ACCESS_TOKEN=... python3 scripts/v2/paras_video_report.py \
      --since 2026-06-26 --out ~/Downloads/paras_video_report.xlsx
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

IST = timezone(timedelta(hours=5, minutes=30))
API = 'https://graph.facebook.com/v19.0'

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    from product_catalogue import derive_category_v2  # noqa: E402
except Exception:  # pragma: no cover
    derive_category_v2 = lambda _n: 'Other'  # noqa: E731

# The operator's seven buckets. offer/collection campaigns win first (they cut
# across products); otherwise the repo's catalogue category maps in.
_CAT_MAP = {
    'Skin': 'Skin', 'Hair': 'Hair', '24K Jewellery': 'Jewellery',
    'Crystal Accessory': 'Crystals', 'Crystal Home Decor': 'Crystal Home Decor',
}
_OFFER = re.compile(r'\boffer\b|collection|sale\d|_sale_?29|combo_?offer|\bdeal\b', re.I)


def categorize(name: str) -> str:
    if _OFFER.search(name or ''):
        return 'Offer'
    try:
        cat = derive_category_v2(name or '')
    except Exception:
        cat = 'Other'
    return _CAT_MAP.get(cat, 'Others')
REPO = Path(__file__).resolve().parent.parent.parent


# Meta rate-limit / transient error markers. These need MINUTES to clear, not
# seconds — "too many calls" is an account-level throttle.
_RETRY_MSG = ('too many calls', 'temporarily unavailable', 'try again',
              'request limit reached', 'reduce the amount of data', 'unknown error')


def _get(url, retries=8):
    delay = 20
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=120) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            body = ''
            try:
                body = e.read().decode()
            except Exception:
                pass
            msg = (json.loads(body).get('error', {}).get('message', '') if body else '').lower()
            # An ACCOUNT-level throttle ("too many calls to this ad-account")
            # won't clear in seconds — fail fast after 2 tries so the caller can
            # defer this account and move on, instead of grinding for 20 minutes.
            acct_throttle = 'too many calls to this ad-account' in msg
            limit = 2 if acct_throttle else retries
            retryable = (e.code in (500, 503, 429)
                         or any(m in msg for m in _RETRY_MSG))
            if e.code == 403 or not retryable or attempt >= limit - 1:
                raise
            print(f'    rate/transient ({e.code}: {msg[:50]}) — waiting {delay}s '
                  f'[{attempt + 1}/{limit}]', flush=True)
        except (urllib.error.URLError, TimeoutError):
            if attempt == retries - 1:
                raise
        time.sleep(delay)
        delay = min(delay * 2, 300)      # cap at 5 min per wait


def paged(path, token, **params):
    params['access_token'] = token
    url = f'{API}/{path}?{urllib.parse.urlencode(params)}'
    out = []
    while url:
        d = _get(url)
        out += d.get('data', [])
        url = d.get('paging', {}).get('next')
    return out


def configured_accounts() -> dict:
    out = {}
    for name in ('config/accounts.env', '.env'):
        p = REPO / name
        if p.exists():
            for line in p.read_text(errors='ignore').splitlines():
                m = re.match(r'^\s*([A-Z][A-Z0-9_]*)\s*=\s*(act_\d+)\s*$', line.strip())
                if m and m.group(2) not in out.values():
                    out[m.group(1)] = m.group(2)
    return out


def video_id_of(creative: dict) -> str | None:
    """creative.video_id, else object_story_spec.video_data.video_id, else the
    first video in an asset_feed_spec."""
    if not creative:
        return None
    if creative.get('video_id'):
        return str(creative['video_id'])
    oss = creative.get('object_story_spec') or {}
    v = (oss.get('video_data') or {}).get('video_id')
    if v:
        return str(v)
    afs = creative.get('asset_feed_spec') or {}
    vids = afs.get('videos') or []
    if vids and vids[0].get('video_id'):
        return str(vids[0]['video_id'])
    return None


def batch_ids(ids, fields, token):
    """GET ?ids=a,b,c&fields=... in chunks of 50 (Meta's cap)."""
    out = {}
    ids = list(ids)
    for i in range(0, len(ids), 50):
        chunk = ids[i:i + 50]
        q = urllib.parse.urlencode({'ids': ','.join(chunk), 'fields': fields,
                                    'access_token': token})
        d = _get(f'{API}/?{q}')
        if isinstance(d, dict):
            out.update(d)
    return out


def fetch_paras_ads(aid: str, token: str, since: str) -> list[dict]:
    """Paras-tagged ads created since `since`, with resolved video_id."""
    flt = json.dumps([{'field': 'ad.created_time', 'operator': 'GREATER_THAN', 'value': since}])
    ads = paged(f'{aid}/ads', token, limit=100, filtering=flt,
                fields='name,campaign_id,adset_id,created_time,status,effective_status,'
                       'creative{video_id,object_story_spec,asset_feed_spec}')
    out = []
    for a in ads:
        if 'paras' not in (a.get('name') or '').lower():
            continue
        vid = video_id_of(a.get('creative') or {})
        if not vid:
            continue
        out.append({'ad_id': a['id'], 'name': a['name'], 'campaign_id': a.get('campaign_id'),
                    'created': a.get('created_time', '')[:10], 'video_id': vid,
                    'status': a.get('effective_status', '')})
    return out


def fetch_campaigns(aid: str, token: str) -> dict:
    camps = paged(f'{aid}/campaigns', token, limit=200,
                  fields='name,status,effective_status,start_time,stop_time,created_time')
    return {c['id']: c for c in camps}


def _post(path, token, **params):
    params['access_token'] = token
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(f'{API}/{path}', data=data, method='POST')
    for attempt in range(6):
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 403 or attempt == 5:
                raise
            time.sleep(min(20 * 2 ** attempt, 300))


def fetch_daily_ad_insights(aid: str, token: str, since: str, until: str) -> dict:
    """{ad_id: {date: (spend, revenue)}} — daily ad-level, via ASYNC insights.

    A synchronous time_increment=1 pull over 27 days paginates hard and trips
    the development-tier throttle. The async report runs server-side and is
    fetched in a few paged reads, which is far kinder to the rate limit.
    """
    job = _post(f'{aid}/insights', token, level='ad', time_increment=1,
                time_range=json.dumps({'since': since, 'until': until}),
                fields='ad_id,spend,action_values')
    run_id = job['report_run_id']
    for _ in range(60):                       # poll up to ~5 min
        st = _get(f'{API}/{run_id}?' + urllib.parse.urlencode({'access_token': token}))
        if st.get('async_status') == 'Job Completed':
            break
        if st.get('async_status') == 'Job Failed':
            raise RuntimeError(f'async insights failed for {aid}')
        time.sleep(5)
    rows = paged(f'{run_id}/insights', token, limit=500)
    out: dict = defaultdict(dict)
    for r in rows:
        rev = 0.0
        for a in (r.get('action_values') or []):
            if a.get('action_type') == 'omni_purchase':
                rev = float(a.get('value') or 0)
                break
        out[r['ad_id']][r['date_start']] = (float(r.get('spend') or 0), rev)
    return out


def roas(rev, spend):
    return round(rev / spend, 2) if spend else 0.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--since', default='2026-06-26')
    ap.add_argument('--until', default=datetime.now(IST).strftime('%Y-%m-%d'))
    ap.add_argument('--accounts', nargs='*', default=None,
                    help='act_ ids to limit to (default: all configured)')
    ap.add_argument('--out', default=str(Path.home() / 'Downloads' / 'paras_video_report.xlsx'))
    args = ap.parse_args()
    token = os.environ['META_ACCESS_TOKEN']

    accts = {v: k for k, v in configured_accounts().items()}
    if args.accounts:
        accts = {a: accts.get(a, a) for a in args.accounts}
    # act_466.. (SM Fragrance) and act_1181.. (SM Crystals) were rate-limited
    # during development; order them last so they have the longest cool-down.
    _last = ('act_466922745634023', 'act_1181596092752041')
    accts = {**{k: v for k, v in accts.items() if k not in _last},
             **{k: v for k, v in accts.items() if k in _last}}
    print(f'{len(accts)} accounts · {args.since} .. {args.until}')

    ads, camps, daily, acct_of = [], {}, {}, {}

    def do_account(aid, aname):
        # 1) delivering ads (with daily spend/rev) — this is the cheap filter:
        #    an account that created 1500 ads but ran 2 returns 2 here, so we
        #    never page thousands of never-delivered ads.
        acct_daily = fetch_daily_ad_insights(aid, token, args.since, args.until)
        if not acct_daily:
            print(f'  {aname:20} no delivery in range', flush=True); return
        # 2) details for exactly those ad_ids, in batches of 50
        meta = batch_ids(acct_daily.keys(),
                         'name,campaign_id,created_time,effective_status,'
                         'creative{video_id,object_story_spec,asset_feed_spec}', token)
        acct_ads = []
        for ad_id, m in meta.items():
            if not isinstance(m, dict) or 'paras' not in (m.get('name') or '').lower():
                continue
            vid = video_id_of(m.get('creative') or {})
            if not vid:
                continue
            acct_ads.append({'ad_id': ad_id, 'name': m['name'],
                             'campaign_id': m.get('campaign_id'),
                             'created': m.get('created_time', '')[:10], 'video_id': vid,
                             'status': m.get('effective_status', '')})
        # 3) campaigns those ads belong to (batch)
        cids = {a['campaign_id'] for a in acct_ads if a.get('campaign_id')}
        cmeta = batch_ids(cids, 'name,status,effective_status,start_time,stop_time,created_time', token)
        for cid, c in cmeta.items():
            if isinstance(c, dict):
                camps[cid] = c
        for a in acct_ads:
            acct_of[a['ad_id']] = aname
            daily[a['ad_id']] = acct_daily.get(a['ad_id'], {})
        ads.extend(acct_ads)
        print(f'  {aname:20} {len(acct_daily)} delivering · {len(acct_ads)} paras video-ads', flush=True)

    # A throttled account is DEFERRED to the end rather than blocking the rest —
    # it cools down while the others run, instead of pinning its quota with retries.
    deferred, queue = [], list(accts.items())
    for aid, aname in queue:
        try:
            do_account(aid, aname)
        except urllib.error.HTTPError as e:
            if e.code == 403:
                print(f'  {aname}: SKIP — 403 (permission)', flush=True); continue
            print(f'  {aname}: throttled, deferring to the end', flush=True)
            deferred.append((aid, aname))
        except Exception as e:
            print(f'  {aname}: error ({str(e)[:60]}), deferring', flush=True)
            deferred.append((aid, aname))
        time.sleep(10)
    for aid, aname in deferred:
        print(f'  retrying deferred: {aname}', flush=True)
        try:
            do_account(aid, aname)
        except Exception as e:
            print(f'  {aname}: STILL failing ({str(e)[:60]}) — omitted', flush=True)
        time.sleep(15)

    if not ads:
        print('no paras video ads found'); return

    # ── group ads by video_id ────────────────────────────────────────────
    by_video: dict = defaultdict(list)
    for a in ads:
        by_video[a['video_id']].append(a)

    today = date.fromisoformat(args.until)

    def camp_survival(cid):
        c = camps.get(cid, {})
        st = c.get('start_time') or c.get('created_time')
        if not st:
            return None, None, None
        s = datetime.fromisoformat(st.replace('+0000', '+00:00')).date()
        stop = c.get('stop_time')
        e = (datetime.fromisoformat(stop.replace('+0000', '+00:00')).date()
             if stop else today)
        e = min(e, today)
        return s, e, max(0, (e - s).days)

    video_rows, deploy_rows = [], []
    for vid, vads in by_video.items():
        launch = min(a['created'] for a in vads)
        launch_d = date.fromisoformat(launch)
        rep_name = min((a['name'] for a in vads), key=len)   # shortest = cleanest
        camp_ids = {a['campaign_id'] for a in vads}

        # windowed + total spend/rev over the video's ad-days
        w = {1: [0.0, 0.0], 3: [0.0, 0.0], 7: [0.0, 0.0], 'all': [0.0, 0.0]}
        for a in vads:
            for dstr, (sp, rev) in daily.get(a['ad_id'], {}).items():
                d = date.fromisoformat(dstr)
                w['all'][0] += sp; w['all'][1] += rev
                for n in (1, 3, 7):
                    if launch_d <= d < launch_d + timedelta(days=n):
                        w[n][0] += sp; w[n][1] += rev

        video_rows.append([
            categorize(rep_name), rep_name[:64], vid, len(camp_ids), len(vads), launch,
            round(w['all'][0]), round(w['all'][1]), roas(w['all'][1], w['all'][0]),
            roas(w[1][1], w[1][0]), roas(w[3][1], w[3][0]), roas(w[7][1], w[7][0]),
        ])

        # per-campaign deployment survival + performance
        for cid in camp_ids:
            c = camps.get(cid, {})
            s, e, surv = camp_survival(cid)
            cads = [a for a in vads if a['campaign_id'] == cid]
            sp = rev = 0.0
            for a in cads:
                for _d, (x, y) in daily.get(a['ad_id'], {}).items():
                    sp += x; rev += y
            deploy_rows.append([
                rep_name[:48], vid, (c.get('name') or cid)[:56],
                acct_of.get(cads[0]['ad_id'], ''), min(a['created'] for a in cads),
                str(s) if s else '', str(e) if e else '',
                surv if surv is not None else '',
                c.get('effective_status', ''), round(sp), round(rev), roas(rev, sp),
            ])

    CAT_ORDER = {'Skin':0,'Hair':1,'Jewellery':2,'Crystals':3,'Crystal Home Decor':4,'Offer':5,'Others':6}
    video_rows.sort(key=lambda r: (CAT_ORDER.get(r[0], 9), -r[6]))  # by category, then spend
    deploy_rows.sort(key=lambda r: (r[1], -r[9]))  # by video, then spend

    # ── write xlsx ───────────────────────────────────────────────────────
    from xlsx_out import new_workbook, write_table
    wb = new_workbook()
    vh = ['Category', 'Video (name)', 'video_id', 'Tries (campaigns)', 'Ads', 'First launch',
          'Spend ₹', 'Revenue ₹', 'ROAS', 'ROAS 1d', 'ROAS 3d', 'ROAS 7d']
    ws = wb.create_sheet('Videos')
    write_table(ws, f'PARAS VIDEOS — {args.since} to {args.until} · one row per video · '
                    f'"Tries" = distinct campaigns the video ran in · ROAS 1d/3d/7d = first N '
                    f'days since the video first launched',
                vh, video_rows, numfmt={7: '#,##0', 8: '#,##0', 9: '0.00', 10: '0.00',
                                        11: '0.00', 12: '0.00'},
                center_cols={1, 4, 5, 6, 9, 10, 11, 12})

    # category summary
    from collections import Counter
    csum = defaultdict(lambda: [0, 0, 0.0, 0.0])   # videos, tries, spend, rev
    for r in video_rows:
        c = csum[r[0]]; c[0] += 1; c[1] += r[3]; c[2] += r[6]; c[3] += r[7]
    srows = [[k, v[0], v[1], round(v[2]), round(v[3]), roas(v[3], v[2])]
             for k, v in sorted(csum.items(), key=lambda x: CAT_ORDER.get(x[0], 9))]
    tot = [sum(c[i] for c in csum.values()) for i in range(4)]
    srows.append(['TOTAL', tot[0], tot[1], round(tot[2]), round(tot[3]), roas(tot[3], tot[2])])
    wsc = wb.create_sheet('By Category')
    write_table(wsc, 'BY CATEGORY — Paras videos', ['Category', 'Videos', 'Total tries',
                'Spend ₹', 'Revenue ₹', 'ROAS'], srows,
                numfmt={4: '#,##0', 5: '#,##0', 6: '0.00'}, center_cols={2, 3, 6})

    dh = ['Video', 'video_id', 'Campaign', 'Account', 'First ad', 'Camp start',
          'Camp end', 'Survived (days)', 'Status', 'Spend ₹', 'Revenue ₹', 'ROAS']
    ws2 = wb.create_sheet('Deployments')
    write_table(ws2, 'DEPLOYMENTS — one row per (video × campaign) · "Survived" = start to '
                     'stop, or to today if still running',
                dh, deploy_rows, numfmt={10: '#,##0', 11: '#,##0', 12: '0.00'},
                center_cols={5, 6, 7, 8, 9, 12})

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    wb.save(args.out)
    nv, nc = len(video_rows), len({a['campaign_id'] for a in ads})
    print(f'\n{len(ads)} paras video-ads · {nv} distinct videos · {nc} campaigns')
    print(f'wrote {args.out}')


if __name__ == '__main__':
    main()
