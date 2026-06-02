"""Invoke Claude Code in headless mode (uses your Max OAuth from Keychain — no API tokens billed).

`claude -p <prompt> --output-format json` runs one turn and prints a JSON result.
We grant it bypassPermissions so it can read/run scripts in REPORTS_REPO without prompts.
"""
from __future__ import annotations

import json
import os
import subprocess
import logging
from textwrap import dedent

log = logging.getLogger(__name__)


SYSTEM_PROMPT = dedent("""
    You are Antriksh, Pulkit Sharma's Meta Ads operator for the NTN team (Studd Muffyn / SML / NBP).
    A team member just messaged you on WhatsApp. Their numbers are in the allowlist; treat them as trusted.

    Capabilities:
    - You have full read access to ~/meta-ads-reports — scripts/, config/, docs/, state/, out/.
    - You can run scripts (e.g. `python3 scripts/live_camp_monitor.py`) to fetch fresh data.
    - The slash commands /today, /closing, /categories, /kpi, /spend, /portal-roas, /dashboard, /help
      already exist as direct script runs. If the request maps to one of those, just point the user
      at the slash command instead of doing it yourself.

    Constraints (HARD):
    - **Never modify Meta Ads** (pause, budget, create, edit). If asked, refuse politely and tell them
      to get Pulkit's approval in the NTN Ads Execution Squad group first.
    - **WhatsApp output rules**: keep replies under 1500 chars. No huge tables — use short lines,
      simple emojis, link to sheets/dashboards for detail. Always sign off with the IST timestamp.
    - **No long stack traces.** Summarize errors in one line.

    Format conventions:
    - ROAS: `1D 2.3x | 7D 1.8x`
    - Currency: `₹` + Indian formatting (`₹1,23,456`)
    - Categories: Skin / Hair / Crystal / Jewellery / Nutraceuticals / Fragrance / Clocks / Frames

    Now reply to the team member's message below.
""").strip()


def run_claude(user_message: str, sender: str, timeout: int = 240) -> str:
    """Run `claude -p` and return the assistant's text reply."""
    claude_bin = os.environ.get("CLAUDE_BIN", "claude")
    repo = os.environ["REPORTS_REPO"]

    prompt = f"[from WhatsApp +{sender}] {user_message}"

    cmd = [
        claude_bin,
        "-p", prompt,
        "--output-format", "json",
        "--permission-mode", "bypassPermissions",
        "--append-system-prompt", SYSTEM_PROMPT,
        "--add-dir", repo,
    ]
    log.info("claude -p invoked for sender=%s msg_len=%d", sender, len(user_message))
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=repo)
    except subprocess.TimeoutExpired:
        return "⏱️ Claude took too long. Try a slash command (/help) or rephrase."
    except FileNotFoundError:
        return "❌ Claude Code CLI not found. Check CLAUDE_BIN in .env."
    if proc.returncode != 0:
        log.error("claude exit=%s stderr=%s", proc.returncode, (proc.stderr or "")[-500:])
        return "❌ Claude returned an error. Try /help for available commands."

    try:
        result = json.loads(proc.stdout)
    except json.JSONDecodeError:
        # Fall back to raw stdout
        return (proc.stdout or "").strip()[:1500] or "⚠️ Empty response."

    # The headless JSON shape varies by claude version. Try common keys.
    text = (
        result.get("result")
        or result.get("text")
        or result.get("response")
        or result.get("output")
        or ""
    )
    if isinstance(text, list):
        text = "\n".join(str(t) for t in text)
    text = (str(text) or "").strip()
    return text[:1500] if text else "⚠️ Empty response from Claude."
