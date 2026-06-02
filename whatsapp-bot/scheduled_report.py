"""Scheduled daily report — runs a script, sends output to a list of WhatsApp recipients.

Invoked by launchd at the time configured in com.ntn.whatsapp-daily-report.plist.

Configurable via .env:
- DAILY_REPORT_SCRIPT  : path (relative to scripts/) of the report to run
                          default: today_live_report.py
- DAILY_REPORT_RECIPIENTS : comma-separated E.164 numbers (no +)
- DAILY_REPORT_TEMPLATE : Meta template name (production); if blank, free-form text used
- DAILY_REPORT_TEMPLATE_LANG : language code (default en)

Free-form text only delivers if recipient is in a 24h conversation window (they messaged
the bot recently). For production, create a Meta utility template and set DAILY_REPORT_TEMPLATE.
"""
from __future__ import annotations

import os
import sys
import logging
import subprocess
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
import requests

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")

# Chain in scripts' env (META_ACCESS_TOKEN, sheet IDs, etc.)
_extra = os.environ.get("EXTRA_ENV_FILE")
if _extra and Path(_extra).expanduser().is_file():
    load_dotenv(Path(_extra).expanduser(), override=False)
_repo = os.environ.get("REPORTS_REPO", str(ROOT.parent))
for cfg in ("config/accounts.env", "config/sheets.env"):
    p = Path(_repo) / cfg
    if p.is_file():
        load_dotenv(p, override=False)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s daily :: %(message)s",
    handlers=[logging.FileHandler(ROOT / "scheduled_report.log"), logging.StreamHandler()],
)
log = logging.getLogger("daily")


def _now_ist() -> str:
    return datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%d %b %Y, %H:%M IST")


def _graph(path: str) -> str:
    return f"https://graph.facebook.com/{os.environ.get('WA_GRAPH_VERSION','v21.0')}/{path}"


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {os.environ['WA_ACCESS_TOKEN']}",
        "Content-Type": "application/json",
    }


def run_report(script_rel: str, timeout: int = 240) -> str:
    """Run report script, return its stdout (last ~3500 chars to fit WhatsApp).

    Special token `__USE_SKU__` runs whatsapp-bot/sku_units_report.py (bundled with the bot,
    not in the main scripts/ tree). Anything else is resolved under scripts/.
    """
    if script_rel == "__USE_SKU__":
        script = ROOT / "sku_units_report.py"
        cwd = ROOT
        # Use the bot's venv python so dotenv/requests etc. are available
        py = str(ROOT / ".venv" / "bin" / "python")
    else:
        script = Path(_repo) / "scripts" / script_rel
        cwd = _repo
        py = "python3"
    if not script.exists():
        return f"❌ Script not found: {script_rel}"
    log.info("running %s", script)
    try:
        proc = subprocess.run(
            [py, str(script)],
            capture_output=True, text=True, timeout=timeout, cwd=cwd,
        )
    except subprocess.TimeoutExpired:
        return f"⏱️ {script_rel} timed out after {timeout}s"
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    if proc.returncode != 0:
        return f"❌ {script_rel} failed (exit {proc.returncode}):\n{err[-600:]}"
    if not out:
        return f"⚠️ {script_rel} produced no output."
    return out[-3500:]


def send_text(to: str, body: str) -> bool:
    """Free-form text. Works only inside 24h conversation window."""
    if len(body) > 4090:
        body = body[:4080] + "\n…(truncated)"
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"preview_url": False, "body": body},
    }
    r = requests.post(_graph(f"{os.environ['WA_PHONE_NUMBER_ID']}/messages"),
                      headers=_headers(), json=payload, timeout=30)
    ok = r.ok
    if not ok:
        log.warning("send_text %s failed: %s %s", to, r.status_code, r.text[:300])
    return ok


def send_template(to: str, template_name: str, params: list[str], lang: str = "en") -> bool:
    """Template message. Works any time once template is approved."""
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": lang},
            "components": [{
                "type": "body",
                "parameters": [{"type": "text", "text": p} for p in params],
            }],
        },
    }
    r = requests.post(_graph(f"{os.environ['WA_PHONE_NUMBER_ID']}/messages"),
                      headers=_headers(), json=payload, timeout=30)
    ok = r.ok
    if not ok:
        log.warning("send_template %s failed: %s %s", to, r.status_code, r.text[:300])
    return ok


def main():
    recipients_raw = os.environ.get("DAILY_REPORT_RECIPIENTS", "")
    recipients = [n.strip().lstrip("+") for n in recipients_raw.split(",") if n.strip()]
    if not recipients:
        log.error("No DAILY_REPORT_RECIPIENTS configured in .env"); sys.exit(2)

    script_rel = os.environ.get("DAILY_REPORT_SCRIPT", "today_live_report.py")
    template = (os.environ.get("DAILY_REPORT_TEMPLATE") or "").strip()
    lang = os.environ.get("DAILY_REPORT_TEMPLATE_LANG", "en")

    log.info("recipients=%d script=%s template=%r", len(recipients), script_rel, template)

    report_body = run_report(script_rel)
    header = f"📊 *Daily Meta Ads Report* — {_now_ist()}\n\n"
    full = header + report_body

    # Meta template params can't contain newlines/tabs or >4 consecutive spaces.
    # Flatten the multi-line report into a single-line summary for template use.
    import re
    flat = report_body.replace("\t", " ")
    # Convert bullet lines like "• 1271 × NTN1290 — VELORE Snake Chain" into " · 1271×VELORE Snake Chain"
    flat_lines = [l.strip(" •*") for l in flat.split("\n") if l.strip()]
    flat = " · ".join(flat_lines)
    flat = re.sub(r"\s+", " ", flat).strip()
    # Hard cap so we stay within Meta's 1024-char body + 60-char param soft limits
    if len(flat) > 900:
        flat = flat[:895] + " …"

    sent, failed = 0, []
    for num in recipients:
        if template:
            ok = send_template(num, template, [_now_ist(), flat], lang=lang)
        else:
            ok = send_text(num, full)
        if ok:
            sent += 1
            log.info("✅ sent to %s", num)
        else:
            failed.append(num)
            log.warning("❌ failed for %s (likely no 24h window — recipient must DM bot first, or use template)", num)
    log.info("done: %d sent, %d failed (%s)", sent, len(failed), ",".join(failed) if failed else "none")


if __name__ == "__main__":
    main()
