"""Dispatch an incoming text message to either a slash command or Claude Code."""
from __future__ import annotations

import logging
import shlex

from commands import COMMANDS
from claude_runner import run_claude

log = logging.getLogger(__name__)


def route(text: str, sender: str) -> str:
    """Return the reply string for an inbound message."""
    text = (text or "").strip()
    if not text:
        return "🤔 Empty message. Type /help for commands."

    # Slash command path — first whitespace-separated token starting with "/"
    if text.startswith("/"):
        try:
            tokens = shlex.split(text)
        except ValueError:
            tokens = text.split()
        cmd = tokens[0].lower()
        args = tokens[1:]
        handler = COMMANDS.get(cmd)
        if handler:
            log.info("slash command: %s args=%s sender=%s", cmd, args, sender)
            try:
                return handler(args)
            except Exception as e:
                log.exception("command %s raised", cmd)
                return f"❌ {cmd} failed: {e}"
        # Unknown slash command — show /help instead of guessing
        return f"❓ Unknown command: {cmd}\n\n" + COMMANDS["/help"]([])

    # Natural language path — hand off to Claude Code
    log.info("NL message sender=%s len=%d", sender, len(text))
    return run_claude(text, sender)
