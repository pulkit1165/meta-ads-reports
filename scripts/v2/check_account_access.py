#!/usr/bin/env python3
"""
check_account_access.py — which ad accounts can META_ACCESS_TOKEN actually read?

Run this whenever spend looks low or a portal goes quiet. It cross-checks every
account in config/accounts.env against what the token can really reach, and
tells you which ones are silently missing.

Background: the production token is a Business Manager SYSTEM USER token. A
system user can only reach assets EXPLICITLY ASSIGNED to it in Business
settings — `ads_read` granted "on all accounts" is a permission scope, not an
assignment. So a newly created or newly moved ad account is invisible until
someone adds it to the system user, and Meta reports that as a plain 403 that
the pipeline used to swallow. That is how ~Rs1L/day of NBP Skin spend went
missing from every report without a single error.

Usage:
  META_ACCESS_TOKEN=... python3 scripts/v2/check_account_access.py
  ...                   python3 scripts/v2/check_account_access.py --token-file ~/.env
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API = 'https://graph.facebook.com/v19.0'
REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def api(path: str, token: str, **params):
    params['access_token'] = token
    url = f"{API}/{path}?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(url, timeout=40) as r:
            return json.load(r), None
    except urllib.error.HTTPError as e:
        try:
            return None, json.loads(e.read().decode()).get('error', {}).get('message', str(e))
        except Exception:
            return None, f'HTTP {e.code}'
    except Exception as e:
        return None, str(e)


def configured_accounts() -> dict:
    """{name: act_id} from config/accounts.env (plus any .env in the repo root)."""
    out = {}
    for p in (REPO_ROOT / 'config' / 'accounts.env', REPO_ROOT / '.env'):
        if not p.exists():
            continue
        for line in p.read_text(errors='ignore').splitlines():
            m = re.match(r'^\s*([A-Z][A-Z0-9_]*)\s*=\s*(act_\d+)\s*$', line.strip())
            if m:
                out.setdefault(m.group(1), m.group(2))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--token', default=os.environ.get('META_ACCESS_TOKEN', ''))
    ap.add_argument('--token-file', default=None,
                    help='read META_ACCESS_TOKEN from this .env file')
    args = ap.parse_args()

    token = args.token
    if args.token_file:
        for line in Path(args.token_file).expanduser().read_text(errors='ignore').splitlines():
            if line.startswith('META_ACCESS_TOKEN='):
                token = line.split('=', 1)[1].strip().strip('"\'')
    if not token:
        print('FATAL: no META_ACCESS_TOKEN'); sys.exit(1)

    # 1) token identity
    d, err = api('debug_token', token, input_token=token)
    if err:
        print(f'FATAL: token invalid — {err}'); sys.exit(1)
    info = d.get('data', {})
    exp = info.get('expires_at', 0)
    print(f"token: type={info.get('type')} app={info.get('application')} "
          f"expires={'never' if exp == 0 else exp} valid={info.get('is_valid')}")
    print(f"scopes: {','.join(info.get('scopes', []))}\n")

    # 2) what the token can discover on its own
    visible, cursor = {}, None
    while True:
        d, err = api('me/adaccounts', token, fields='account_id,name', limit=200,
                     **({'after': cursor} if cursor else {}))
        if err or not d:
            print(f'  warn: me/adaccounts failed — {err}')
            break
        for a in d.get('data', []):
            visible[f"act_{a['account_id']}"] = a.get('name', '')
        cursor = d.get('paging', {}).get('cursors', {}).get('after')
        if not d.get('paging', {}).get('next'):
            break
    print(f"discoverable via me/adaccounts: {len(visible)} accounts")

    # 3) every configured account, probed directly — discovery can miss an
    #    account that direct access still allows, and vice versa.
    cfg = configured_accounts()
    print(f"configured in config/accounts.env: {len(cfg)} accounts\n")
    ok, broken = [], []
    for name, aid in sorted(cfg.items()):
        d, err = api(f'{aid}/campaigns', token, fields='name', limit=1)
        (ok if err is None else broken).append((name, aid, err))

    for name, aid, _ in ok:
        tag = '' if aid in visible else '   (reachable, but NOT in me/adaccounts)'
        print(f"  OK       {name:22} {aid}{tag}")
    if broken:
        print()
        for name, aid, err in broken:
            print(f"  BLOCKED  {name:22} {aid}\n           -> {err[:150]}")

    print(f"\n{len(ok)}/{len(cfg)} configured accounts readable.")
    if broken:
        print(f"\n{len(broken)} account(s) are INVISIBLE to every report. Their spend is\n"
              "missing from the dashboard, the blended ROAS, and the closing report.\n"
              "Fix: business.facebook.com -> Business settings -> Users -> System users\n"
              f"     -> '{info.get('application', 'your system user')}' -> Add assets -> Ad accounts\n"
              "     -> tick the account(s) above -> grant 'Manage campaigns' -> Save.\n"
              "No new token is needed; the existing one picks them up immediately.")
        sys.exit(2)


if __name__ == '__main__':
    main()
