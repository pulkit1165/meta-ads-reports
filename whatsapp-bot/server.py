"""Flask webhook server for Meta WhatsApp Cloud API.

Meta hits two endpoints:
  GET  /webhook  → verification handshake (returns hub.challenge if token matches)
  POST /webhook  → message events (text, status updates, etc.)

Run with `./run.sh` after `cp .env.example .env` and filling in values.
"""
from __future__ import annotations

import os
import sys
import logging
import threading
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, request, jsonify, abort

# Load .env from this directory (bot config: WA creds, allowlist, paths)
load_dotenv(Path(__file__).parent / ".env")

# Also load a secondary env file if EXTRA_ENV_FILE is set (e.g. the openclaw workspace .env
# that holds META_ACCESS_TOKEN, GOOGLE_SERVICE_ACCOUNT_FILE, sheet IDs etc.).
# `override=False` means bot's own .env wins on any key collision.
_extra = os.environ.get("EXTRA_ENV_FILE")
if _extra and Path(_extra).expanduser().is_file():
    load_dotenv(Path(_extra).expanduser(), override=False)

# Also pre-load the meta-ads-reports config files (committed, non-secret).
_repo = os.environ.get("REPORTS_REPO")
if _repo:
    for cfg in ("config/accounts.env", "config/sheets.env"):
        p = Path(_repo) / cfg
        if p.is_file():
            load_dotenv(p, override=False)

# Ensure local imports work
sys.path.insert(0, str(Path(__file__).parent))
import wa_client  # noqa: E402
from router import route  # noqa: E402


# ── Logging ───────────────────────────────────────────────────────────────

log_file = os.environ.get("LOG_FILE", "server.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    handlers=[logging.FileHandler(log_file), logging.StreamHandler()],
)
log = logging.getLogger("wa-bot")


# ── Helpers ───────────────────────────────────────────────────────────────


def _allowlist() -> set[str]:
    raw = os.environ.get("ALLOWLIST", "")
    return {n.strip().lstrip("+") for n in raw.split(",") if n.strip()}


def _process_message(sender: str, text: str, message_id: str) -> None:
    """Compute reply and send. Runs in a background thread so the webhook can return 200 fast."""
    try:
        wa_client.mark_read(message_id)
    except Exception:
        pass
    try:
        reply = route(text, sender)
    except Exception as e:
        log.exception("router crashed")
        reply = f"❌ Internal error: {e}"
    if not reply:
        reply = "⚠️ No reply produced."
    log.info("→ %s: %s", sender, reply[:120].replace("\n", " "))
    try:
        wa_client.send_text(sender, reply)
    except Exception:
        log.exception("send_text failed")


# ── Flask app ─────────────────────────────────────────────────────────────

app = Flask(__name__)


@app.get("/healthz")
def healthz():
    return jsonify({"ok": True, "allowlist_size": len(_allowlist())})


@app.get("/webhook")
def webhook_verify():
    """Meta verification handshake."""
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    expected = os.environ.get("WA_VERIFY_TOKEN")
    if mode == "subscribe" and token and expected and token == expected:
        log.info("webhook verified")
        return challenge or "", 200
    log.warning("webhook verify failed: mode=%s token_match=%s", mode, token == expected)
    abort(403)


@app.post("/webhook")
def webhook_event():
    """Receive a WhatsApp event from Meta."""
    payload = request.get_json(silent=True) or {}
    log.debug("event: %s", payload)

    allowlist = _allowlist()
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            for msg in value.get("messages", []):
                sender = (msg.get("from") or "").lstrip("+")
                msg_type = msg.get("type")
                message_id = msg.get("id", "")
                if sender not in allowlist:
                    log.info("dropped (not in allowlist): %s type=%s", sender, msg_type)
                    continue
                if msg_type != "text":
                    log.info("dropped (unsupported type %s) from %s", msg_type, sender)
                    threading.Thread(
                        target=wa_client.send_text,
                        args=(sender, "⚠️ Only text messages supported. Type /help."),
                        daemon=True,
                    ).start()
                    continue
                text = (msg.get("text", {}) or {}).get("body", "")
                log.info("← %s: %s", sender, text[:120].replace("\n", " "))
                threading.Thread(
                    target=_process_message,
                    args=(sender, text, message_id),
                    daemon=True,
                ).start()
    # Always 200 — Meta retries non-200 responses aggressively
    return jsonify({"status": "received"}), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    # Required env sanity check
    missing = [k for k in ("WA_PHONE_NUMBER_ID", "WA_ACCESS_TOKEN", "WA_VERIFY_TOKEN") if not os.environ.get(k)]
    if missing:
        log.error("Missing required env vars: %s. Copy .env.example to .env and fill them in.", missing)
        sys.exit(1)
    log.info("starting on :%d  allowlist=%d  reports_repo=%s", port, len(_allowlist()), os.environ.get("REPORTS_REPO"))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
