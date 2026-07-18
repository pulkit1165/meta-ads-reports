#!/usr/bin/env python3
"""
wa_notify.py — best-effort WhatsApp delivery for the automated reports.

Same Cloud API surface the bot uses (whatsapp-bot/wa_client.py), lifted here so
report scripts in scripts/v2/ don't have to import across the bot's venv.

DELIVERY REALITY — read before wondering why nothing arrived:
  * Free-form text only lands if the recipient messaged the bot in the last 24h.
  * Outside that window Meta requires an APPROVED TEMPLATE. Set WA_REPORT_TEMPLATE
    to its name and we send template-style with [timestamp, one-line summary].
  * Template body params cannot contain newlines, tabs, or 4+ consecutive
    spaces — flatten() below enforces that.

Never raises: a WhatsApp outage must not fail the report or, more importantly,
block the auto-pause run that follows it.
"""
from __future__ import annotations

import os
import re

import requests

_TIMEOUT = 30


def _graph(path: str) -> str:
    return f"https://graph.facebook.com/{os.environ.get('WA_GRAPH_VERSION', 'v21.0')}/{path}"


def _headers() -> dict:
    return {'Authorization': f"Bearer {os.environ['WA_ACCESS_TOKEN']}",
            'Content-Type': 'application/json'}


def flatten(body: str, limit: int = 900) -> str:
    """Collapse a multi-line report into a single template-safe parameter."""
    lines = [ln.strip(' •*\t') for ln in body.split('\n') if ln.strip()]
    flat = re.sub(r'\s+', ' ', ' · '.join(lines)).strip()
    return flat[:limit - 2] + ' …' if len(flat) > limit else flat


def recipients(env_var: str = 'WA_REPORT_RECIPIENTS') -> list[str]:
    return [n.strip().lstrip('+') for n in os.environ.get(env_var, '').split(',') if n.strip()]


def send(body: str, to: list[str] | None = None, header: str = '') -> tuple[int, int]:
    """Send `body` to every recipient. Returns (sent, failed).

    Silently no-ops when WhatsApp isn't configured, so local runs and CI without
    the secrets behave the same as a normal report run.
    """
    to = to if to is not None else recipients()
    if not to or not os.environ.get('WA_ACCESS_TOKEN') or not os.environ.get('WA_PHONE_NUMBER_ID'):
        return (0, 0)

    template = (os.environ.get('WA_REPORT_TEMPLATE') or '').strip()
    lang = os.environ.get('WA_REPORT_TEMPLATE_LANG', 'en')
    full = (header + '\n\n' + body) if header else body
    if len(full) > 4090:
        full = full[:4080] + '\n…(truncated)'

    sent = failed = 0
    for num in to:
        if template:
            payload = {
                'messaging_product': 'whatsapp', 'to': num, 'type': 'template',
                'template': {
                    'name': template, 'language': {'code': lang},
                    'components': [{'type': 'body', 'parameters': [
                        {'type': 'text', 'text': flatten(header or 'Report', 60)},
                        {'type': 'text', 'text': flatten(body)},
                    ]}],
                },
            }
        else:
            payload = {
                'messaging_product': 'whatsapp', 'to': num, 'type': 'text',
                'text': {'preview_url': False, 'body': full},
            }
        try:
            r = requests.post(_graph(f"{os.environ['WA_PHONE_NUMBER_ID']}/messages"),
                              headers=_headers(), json=payload, timeout=_TIMEOUT)
            if r.ok:
                sent += 1
            else:
                failed += 1
                print(f"  wa: {num} failed {r.status_code} {r.text[:200]}")
        except Exception as e:
            failed += 1
            print(f"  wa: {num} raised {e}")
    return (sent, failed)
