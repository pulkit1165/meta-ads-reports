"""
Daily Report Pipeline — runs at 4:30 AM IST
Runs all 4 reports for yesterday:
  1. Campaign Tracker (15 accounts → SM/SML/NBP tabs)
  2. Budget Reports (Category / Audience / Product)
  3. Creative Report (ad-level creative performance)

Output: WhatsApp summary to Pulkit
"""

import os, sys, subprocess
from datetime import datetime, timedelta
from pathlib import Path

# ── Path setup (Phase 2: GitHub-Actions-friendly paths) ──────────────────────
REPO_ROOT   = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / 'scripts'
ENV_FILE    = str(REPO_ROOT / '.env')

# ── Load token ─────────────────────────────────────────────────────────────────
def load_env():
    env = {}
    if os.path.exists(ENV_FILE):
        with open(ENV_FILE) as f:
            for line in f:
                line = line.strip()
                if '=' in line and not line.startswith('#'):
                    k, v = line.split('=', 1)
                    env[k.strip()] = v.strip()
    return env

ENV = load_env()
# Env vars from the process environment (e.g. GitHub Actions secrets) take
# precedence over .env file values.
TOKEN = os.environ.get('META_ACCESS_TOKEN') or ENV.get('META_ACCESS_TOKEN', '')

# ── Yesterday ─────────────────────────────────────────────────────────────────
YESTERDAY = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
DATE_LABEL = (datetime.now() - timedelta(days=1)).strftime('%d %b %Y')

errors = []

def run(cmd, label, timeout=300):
    env = os.environ.copy()
    env['META_ACCESS_TOKEN'] = TOKEN
    r = subprocess.run(
        cmd, shell=True, capture_output=True, text=True,
        timeout=timeout, env=env, cwd=str(REPO_ROOT)
    )
    if r.returncode != 0:
        errors.append(f"{label}: {r.stderr[-200:]}")
        return False
    return True

# ── Step 1: Campaign Tracker ───────────────────────────────────────────────────
# Use --all (single invocation). The per-account loop was a bug: the builder
# clears the portal tab on every write, so the last account to run wiped all
# preceding accounts' campaigns. --all collects every account's campaigns into
# one per-portal dict keyed by campaign_id before writing, which is what the
# master doc actually prescribes.
print(f"=== Step 1: Campaign Tracker ({YESTERDAY}) — all accounts in one pass ===")
ok1 = run(
    f"python3 {SCRIPTS_DIR}/campaign_tracker_builder.py --all --date {YESTERDAY}",
    "tracker_all",
    timeout=900,  # 15 accounts x ~20s each = allow 15 min
)
print(f"  {'✅' if ok1 else '❌'} Campaign Tracker (SM + SML + NBP)")

# ── Step 2: Budget Reports ─────────────────────────────────────────────────────
print("\n=== Step 2: Budget Reports ===")
ok2 = run(f"python3 {SCRIPTS_DIR}/campaign_tracker_reports.py --date {YESTERDAY}", "budget_reports")
print(f"  {'✅' if ok2 else '❌'} Budget Reports (Category / Audience / Product)")

# ── Step 3: Creative Report ────────────────────────────────────────────────────
print("\n=== Step 3: Creative Report ===")
ok3 = run(f"python3 {SCRIPTS_DIR}/creative_report.py --date {YESTERDAY}", "creative_report")
print(f"  {'✅' if ok3 else '❌'} Creative Report")

# ── Step 4: Closing Camps Report ──────────────────────────────────────────────
print("\n=== Step 4: Closing Camps Report ===")
ok4 = run(f"python3 {SCRIPTS_DIR}/closing_camps_report.py --date {YESTERDAY}", "closing_camps")
print(f"  {'✅' if ok4 else '❌'} Closing Camps Report (1D ROAS < 1.0)")

# ── Step 5: Summary ────────────────────────────────────────────────────────────
# NOTE: Closing Camps Report runs separately at 1:00 PM IST via its own cron job
# Script: closing_camps_report.py --date TODAY
msg = f"""📊 *Daily Reports — {DATE_LABEL}*

{'✅' if ok1 else '❌'} Campaign Tracker: SM + SML + NBP (de-duped across all accounts)
{'✅' if ok2 else '❌'} Budget Reports: Category + Audience + Product
{'✅' if ok3 else '❌'} Creative Report: Paras / Motion / Static / Partnership / Catalogue / Testing
{'✅' if ok4 else '❌'} Closing Camps: 1D ROAS < 1.0 watchlist

🔗 https://docs.google.com/spreadsheets/d/{os.environ.get('REPORTS_SHEET_ID', '1hJ3IS2VDtTAEyyJIV__jvts9CMQdYhyxKAfWKtrkUH4')}"""

if errors:
    msg += f"\n\n⚠️ *Errors ({len(errors)}):*\n" + "\n".join(errors[:5])

print("\n" + msg)
