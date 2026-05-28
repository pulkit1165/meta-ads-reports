#!/usr/bin/env python3
"""
Ad-hoc analysis: for top-spending ads in the last N days, fetch each ad's
video duration from Meta and bucket performance (spend, revenue, ROAS) by
length. Answers "which video duration is working best?"

Reads top ads from state/ntn.db, fetches creative.video_id and then
video.length from Meta API (batched 50 IDs per call).

Env:
  META_ACCESS_TOKEN  — required
  DAYS               — look-back window (default 40)
  TOP_N              — analyze top N ads by spend (default 150)
"""
import os
import sys
import sqlite3
import requests
from collections import defaultdict
from pathlib import Path

DB = Path(__file__).resolve().parent.parent.parent / 'state' / 'ntn.db'
TOKEN = os.environ.get('META_ACCESS_TOKEN')
if not TOKEN:
    sys.exit("ERROR: META_ACCESS_TOKEN not set")
DAYS = int(os.environ.get('DAYS', '40'))
TOP_N = int(os.environ.get('TOP_N', '150'))
GRAPH = 'https://graph.facebook.com/v19.0'


def meta_get(path, params=None):
    p = dict(params or {})
    p['access_token'] = TOKEN
    r = requests.get(f'{GRAPH}/{path}', params=p, timeout=30)
    return r.json()


def main():
    conn = sqlite3.connect(DB)
    rows = conn.execute(f'''
        SELECT
          m.ad_id, m.ad_name, m.creative_type, m.category, m.product,
          ROUND(SUM(d.spend), 0)  AS spend,
          ROUND(SUM(d.revenue),0) AS revenue,
          SUM(d.purchases)        AS purchases,
          SUM(d.video_thruplay)   AS thruplay
        FROM meta_ads_daily d
        JOIN meta_ads_meta m ON m.ad_id = d.ad_id
        WHERE d.date >= date('now', '-{DAYS} days') AND d.spend > 0
        GROUP BY m.ad_id
        ORDER BY spend DESC
        LIMIT {TOP_N}
    ''').fetchall()
    print(f'Fetched {len(rows)} top ads by spend (last {DAYS} days)')

    # Batch-fetch creative video_id + inline length for each ad.
    # The page-level video_id namespace (16-digit IDs) isn't readable via
    # /<id> with a standard Marketing API token. Try to pull length straight
    # from creative.object_story_spec.video_data — and dump one sample so we
    # can see what Meta actually returns.
    ad_ids = [r[0] for r in rows]
    ad_to_video = {}
    ad_to_len = {}
    print(f'Fetching creative for {len(ad_ids)} ads (with inline video length attempts)...')
    sample_dumped = False
    for i in range(0, len(ad_ids), 50):
        batch = ad_ids[i:i+50]
        data = meta_get('', {
            'ids': ','.join(batch),
            'fields': (
                'creative{'
                  'id,video_id,thumbnail_url,'
                  'object_story_spec{video_data{video_id,call_to_action,title}},'
                  'effective_object_story_id,'
                  'asset_feed_spec{videos{video_id,thumbnail_url}}'
                '}'
            ),
        })
        if 'error' in data:
            print(f'  err: {data["error"].get("message")}'); continue
        for aid, info in data.items():
            if not isinstance(info, dict): continue
            if not sample_dumped:
                import json as _json
                print(f'  SAMPLE creative response for ad {aid}:')
                print('  ' + _json.dumps(info, indent=2)[:800].replace('\n', '\n  '))
                sample_dumped = True
            cr = info.get('creative') or {}
            vid = cr.get('video_id')
            if not vid:
                oss = cr.get('object_story_spec') or {}
                vd = oss.get('video_data') or {}
                vid = vd.get('video_id')
            if not vid:
                afs = cr.get('asset_feed_spec') or {}
                vids = afs.get('videos') or []
                if vids: vid = vids[0].get('video_id')
            if vid:
                ad_to_video[aid] = vid
    print(f'  Found video_id for {len(ad_to_video)} / {len(ad_ids)} ads')

    # Get the account_id for each ad — we need to query advideos via the
    # ad account context. The direct /{video_id} endpoint returns
    # "Application does not have permission" with a standard Marketing API
    # token; /act_<id>/advideos?fields=id,length works.
    placeholders = ','.join(['?'] * len(ad_ids))
    ad_account = dict(conn.execute(
        f'SELECT ad_id, account_id FROM meta_ads_meta WHERE ad_id IN ({placeholders})',
        ad_ids
    ).fetchall())
    accounts_seen = set(filter(None, ad_account.values()))
    print(f'Listing advideos across {len(accounts_seen)} ad account(s)...')

    video_to_len = {}
    for acct in accounts_seen:
        # Paginate the advideos listing — accounts can have thousands of
        # videos. We page until done OR until we've covered all video_ids
        # we care about.
        next_url = f'{acct}/advideos'
        next_params = {'fields': 'id,length', 'limit': 200}
        page = 0
        while page < 50:   # hard cap on pagination
            r = requests.get(f'{GRAPH}/{next_url}', params={**next_params, 'access_token': TOKEN}, timeout=30)
            d = r.json()
            if 'error' in d:
                print(f'  {acct}: {d["error"].get("message")}'); break
            got = 0
            for v in d.get('data', []) or []:
                vid = v.get('id')
                ln  = v.get('length')
                if vid and ln is not None:
                    try: video_to_len[vid] = float(ln); got += 1
                    except (TypeError, ValueError): pass
            paging = d.get('paging', {})
            next_after = paging.get('cursors', {}).get('after')
            if not next_after or not d.get('data'):
                break
            next_params = {**next_params, 'after': next_after}
            page += 1
        # Optional: short log so we can see progress per account
        # print(f'  {acct}: {len([v for v in ad_to_video.values() if v in video_to_len])} resolved so far')
    print(f'  Got length for {len(video_to_len)} videos total (account-scoped listing)')

    # Diagnostic — did any of the videos we care about actually show up?
    want = set(ad_to_video.values())
    found = want & set(video_to_len.keys())
    missing = list(want - set(video_to_len.keys()))[:5]
    print(f'  Of {len(want)} videos we want, {len(found)} are in the listing.')
    if missing:
        print(f'  Sample missing video_ids: {missing}')
    sample_listing = list(video_to_len.keys())[:3]
    print(f'  Sample listing video_ids:  {sample_listing}')

    # Bucket
    def bucket(secs):
        if secs is None: return 'unknown'
        if secs < 15:   return '<15s'
        if secs < 30:   return '15-30s'
        if secs < 60:   return '30-60s (≈1min)'
        if secs < 90:   return '60-90s (≈1.5min)'
        if secs < 120:  return '90-120s (≈2min)'
        if secs < 180:  return '120-180s (≈3min)'
        if secs < 300:  return '180-300s (3-5min)'
        return '300s+ (5min+)'

    bucket_stats = defaultdict(lambda: {'spend':0, 'revenue':0, 'pur':0, 'thru':0, 'ads':0})
    ad_results = []
    for r in rows:
        ad_id, name, ct, cat, prod, spend, rev, pur, thru = r
        vid = ad_to_video.get(ad_id)
        length = video_to_len.get(vid) if vid else None
        b = bucket(length)
        s = bucket_stats[b]
        s['spend'] += spend or 0
        s['revenue'] += rev or 0
        s['pur'] += pur or 0
        s['thru'] += thru or 0
        s['ads'] += 1
        ad_results.append({
            'name': name, 'ct': ct, 'cat': cat, 'product': prod,
            'spend': spend, 'rev': rev,
            'roas': (rev/spend) if spend and spend > 0 else 0,
            'length_sec': length, 'bucket': b,
        })

    # Print bucket summary
    print()
    print('═' * 100)
    print(f'DURATION BUCKET PERFORMANCE — top {TOP_N} ads by spend, last {DAYS} days')
    print('═' * 100)
    print(f'{"bucket":<22} {"ads":>5} {"spend":>14} {"revenue":>14} {"ROAS":>7} {"thru/ad":>10}')
    order = ['<15s','15-30s','30-60s (≈1min)','60-90s (≈1.5min)','90-120s (≈2min)',
             '120-180s (≈3min)','180-300s (3-5min)','300s+ (5min+)','unknown']
    for b in order:
        if b not in bucket_stats: continue
        s = bucket_stats[b]
        roas = s['revenue']/s['spend'] if s['spend']>0 else 0
        thrupad = s['thru']/s['ads'] if s['ads']>0 else 0
        print(f'  {b:<20} {s["ads"]:>5} ₹{s["spend"]:>12,.0f} ₹{s["revenue"]:>12,.0f} {roas:>6.2f}x {thrupad:>10,.0f}')

    # Top 30 ads with measured duration
    print()
    print('═' * 100)
    print(f'TOP 30 ADS BY SPEND — last {DAYS} days, with measured video duration')
    print('═' * 100)
    print(f'{"name":<48} {"type":<12} {"dur":>7} {"spend":>10} {"ROAS":>6}')
    ad_results.sort(key=lambda x: -(x['spend'] or 0))
    for a in ad_results[:30]:
        nm = (a['name'] or '')[:47]
        dur = f"{a['length_sec']:.0f}s" if a['length_sec'] else '—'
        print(f'  {nm:<46} {(a["ct"] or ""):<12} {dur:>7} ₹{(a["spend"] or 0):>8,.0f} {a["roas"]:>5.2f}x')


if __name__ == '__main__':
    main()
