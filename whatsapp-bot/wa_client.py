"""Meta WhatsApp Cloud API client — send text messages, mark messages read."""
from __future__ import annotations

import os
import logging
import requests

log = logging.getLogger(__name__)


def _graph_url(path: str) -> str:
    version = os.environ.get("WA_GRAPH_VERSION", "v21.0")
    return f"https://graph.facebook.com/{version}/{path}"


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {os.environ['WA_ACCESS_TOKEN']}",
        "Content-Type": "application/json",
    }


def send_text(to: str, body: str) -> dict:
    """Send a plain-text WhatsApp message. `to` is E.164 without leading +."""
    if len(body) > 4096:
        body = body[:4090] + "\n…(truncated)"
    phone_id = os.environ["WA_PHONE_NUMBER_ID"]
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "text",
        "text": {"preview_url": False, "body": body},
    }
    r = requests.post(_graph_url(f"{phone_id}/messages"), headers=_headers(), json=payload, timeout=30)
    if not r.ok:
        log.error("send_text failed: %s %s", r.status_code, r.text[:500])
    return r.json() if r.headers.get("content-type", "").startswith("application/json") else {}


def mark_read(message_id: str) -> None:
    """Mark an inbound message as read (blue ticks). Best-effort; failures are logged but ignored."""
    phone_id = os.environ["WA_PHONE_NUMBER_ID"]
    payload = {"messaging_product": "whatsapp", "status": "read", "message_id": message_id}
    try:
        r = requests.post(_graph_url(f"{phone_id}/messages"), headers=_headers(), json=payload, timeout=10)
        if not r.ok:
            log.warning("mark_read non-OK: %s %s", r.status_code, r.text[:300])
    except Exception as e:
        log.warning("mark_read raised: %s", e)
