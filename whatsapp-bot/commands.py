"""Slash command registry.

Each command maps to a function that takes (args: list[str]) and returns a string
to send back over WhatsApp. Commands are pure-script (no LLM); they run the
existing meta-ads-reports/scripts/*.py and format the output for chat.

Add new commands by writing a function and registering it in COMMANDS.
"""
from __future__ import annotations

import os
import subprocess
import logging
from pathlib import Path

log = logging.getLogger(__name__)


def _scripts_dir() -> Path:
    return Path(os.environ["REPORTS_REPO"]) / "scripts"


def _run_script(rel_path: str, args: list[str] | None = None, timeout: int = 180) -> str:
    """Run a python script and return its stdout (last 3000 chars to fit WhatsApp)."""
    script = _scripts_dir() / rel_path
    if not script.exists():
        return f"❌ Script not found: {rel_path}"
    cmd = ["python3", str(script)] + (args or [])
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=_scripts_dir().parent)
    except subprocess.TimeoutExpired:
        return f"⏱️ Timed out after {timeout}s: {rel_path}"
    except Exception as e:
        return f"❌ Error running {rel_path}: {e}"
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    if proc.returncode != 0:
        return f"❌ {rel_path} failed (exit {proc.returncode})\n{err[-800:]}"
    if not out:
        return f"⚠️ {rel_path} ran but produced no output."
    return out[-3000:]


# ── Command implementations ──────────────────────────────────────────────


def cmd_help(_args: list[str]) -> str:
    return (
        "🦞 Antriksh — slash commands:\n"
        "/today              Live monitor snapshot\n"
        "/closing            Current closing watchlist\n"
        "/categories [portal]  Category × Sales/Retarget  (sm|sml|nbp)\n"
        "/kpi                Daily KPI summary\n"
        "/spend              Portal ad-spend snapshot\n"
        "/portal-roas        Portal ROAS snapshot\n"
        "/dashboard          v2 dashboard link\n"
        "/ping               Health check\n"
        "/help               This list\n\n"
        "Or just ask in plain English — e.g. 'top creatives for SM Skin'."
    )


def cmd_ping(_args: list[str]) -> str:
    return "pong 🦞"


def cmd_today(_args: list[str]) -> str:
    return _run_script("live_camp_monitor.py")


def cmd_closing(_args: list[str]) -> str:
    return _run_script("closing_watchlist.py")


def cmd_categories(args: list[str]) -> str:
    portal = (args[0].lower() if args else "all").strip()
    if portal not in ("sm", "sml", "nbp", "all"):
        return f"❌ Unknown portal '{portal}'. Use sm | sml | nbp | all."
    extra = [] if portal == "all" else ["--portal", portal]
    return _run_script("category_reports.py", extra)


def cmd_kpi(_args: list[str]) -> str:
    return _run_script("kpi_daily_builder.py")


def cmd_spend(_args: list[str]) -> str:
    return _run_script("active_budget_by_product.py")


def cmd_portal_roas(_args: list[str]) -> str:
    return _run_script("today_live_report.py")


def cmd_dashboard(_args: list[str]) -> str:
    url = os.environ.get("DASHBOARD_URL", "https://desistuddmuffyn.in")
    return f"📊 NTN v2 dashboard:\n{url}"


# ── Registry ─────────────────────────────────────────────────────────────


COMMANDS = {
    "/help": cmd_help,
    "/ping": cmd_ping,
    "/today": cmd_today,
    "/closing": cmd_closing,
    "/categories": cmd_categories,
    "/kpi": cmd_kpi,
    "/spend": cmd_spend,
    "/portal-roas": cmd_portal_roas,
    "/dashboard": cmd_dashboard,
}
