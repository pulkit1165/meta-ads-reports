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

ACCOUNTS = [
    'SM_FRAGRANCE_01', 'SM_SKIN', 'SM_HAIR', 'SM_CRYSTALS',
    'SM_PERFUME', 'SM_CL_05', 'SM_CL_06',
    'SML_SKIN', 'SML_HAIR', 'SML_CRYSTALS', 'SML_CL_06', 'SML_CL_07',
    'NBP_SKIN', 'NBP_HAIR_PERFUME', 'NBP_CRYSTALS',
]

PORTAL_MAP = {
    'SM_FRAGRANCE_01': 'SM', 'SM_SKIN': 'SM', 'SM_HAIR': 'SM',
    'SM_CRYSTALS': 'SM', 'SM_PERFUME': 'SM', 'SM_CL_05': 'SM', 'SM_CL_06': 'SM',
    'SML_SKIN': 'SML', 'SML_HAIR': 'SML', 'SML_CRYSTALS': 'SML',
    'SML_CL_06': 'SML', 'SML_CL_07': 'SML',
    'NBP_SKIN': 'NBP', 'NBP_HAIR_PERFUME': 'NBP', 'NBP_CRYSTALS': 'NBP',
}

results = {'SM': 0, 'SML': 0, 'NBP': 0}
errors = []

def run(cmd, label):
    env = os.environ.copy()
    env['META_ACCESS_TOKEN'] = TOKEN
    r = subprocess.run(
        cmd, shell=True, capture_output=True, text=True,
        timeout=300, env=env, cwd=str(REPO_ROOT)
    )
    if r.returncode != 0:
        errors.append(f"{label}: {r.stderr[-200:]}")
        return False
    return True

# ── Step 1: Campaign Tracker ───────────────────────────────────────────────────
print(f"=== Step 1: Campaign Tracker ({YESTERDAY}) ===")
for acct in ACCOUNTS:
    success = run(
        f"python3 {SCRIPTS_DIR}/campaign_tracker_builder.py --account {acct} --date {YESTERDAY}",
        acct
    )
    if success:
        results[PORTAL_MAP[acct]] += 1
    print(f"  {'✅' if success else '❌'} {acct}")

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
total_tabs = sum(results.values())
msg = f"""📊 *Daily Reports — {DATE_LABEL}*

✅ Campaign Tracker: {total_tabs} tabs
   SM: {results['SM']} | SML: {results['SML']} | NBP: {results['NBP']}
{'✅' if ok2 else '❌'} Budget Reports: Category + Audience + Product
{'✅' if ok3 else '❌'} Creative Report: Paras / Motion / Static / Partnership / Catalogue / Testing
{'✅' if ok4 else '❌'} Closing Camps: 1D ROAS < 1.0 watchlist

🔗 https://docs.google.com/spreadsheets/d/11IAPsJlil75aehYf5IzpSaTCLcAgPk9-57p6ZuPNNQM"""

if errors:
    msg += f"\n\n⚠️ *Errors ({len(errors)}):*\n" + "\n".join(errors[:5])

print("\n" + msg)
