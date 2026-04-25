#!/usr/bin/env bash
# Bootstrap a fresh machine for local execution of meta-ads-reports.
#
# Prereqs the user must place manually before running this script:
#   1. google-service-account.json at the repo root (gitignored)
#   2. META_ACCESS_TOKEN in .env (also gitignored)
#
# Usage:
#   bash scripts/bootstrap_local.sh
set -euo pipefail

cd "$(dirname "$0")/.."   # repo root
REPO_ROOT="$(pwd)"
echo "→ Repo root: $REPO_ROOT"

# ── Step 1: Service account file ───────────────────────────────────────────
SA="$REPO_ROOT/google-service-account.json"
if [[ ! -f "$SA" ]]; then
  echo
  echo "❌ Missing: $SA"
  echo
  echo "  How to get this file onto this machine:"
  echo "  Option A — Mac-to-Mac AirDrop (transient, simplest):"
  echo "    From the source machine, locate ~/.openclaw/workspace/google-service-account.json"
  echo "    AirDrop it to this machine, then move:"
  echo "      mv ~/Downloads/google-service-account.json $SA"
  echo
  echo "  Option B — 1Password / Bitwarden secure note (for re-use across machines):"
  echo "    Save the JSON contents as a secure note on the source machine."
  echo "    On this machine, paste the contents back into $SA"
  echo
  echo "  Option C — encrypted USB:"
  echo "    Copy via physical media. Don't email it."
  echo
  echo "  Treat this file like a password. Do NOT commit it. (.gitignore covers it.)"
  exit 1
fi
chmod 600 "$SA"
echo "✓ Service account file present (chmod 600)"

# Sanity-check it's a valid JSON with the right shape
if ! python3 -c "import json; d=json.load(open('$SA')); assert 'client_email' in d and 'private_key' in d" 2>/dev/null; then
  echo "❌ $SA is not a valid Google service-account JSON. Re-copy it."
  exit 1
fi
SA_EMAIL="$(python3 -c "import json; print(json.load(open('$SA'))['client_email'])")"
echo "  Service account: $SA_EMAIL"

# ── Step 2: .env ────────────────────────────────────────────────────────────
ENV_FILE="$REPO_ROOT/.env"
if [[ ! -f "$ENV_FILE" ]]; then
  cp "$REPO_ROOT/.env.example" "$ENV_FILE"
  echo "✓ Created $ENV_FILE from .env.example"
fi

if ! grep -q '^META_ACCESS_TOKEN=..*' "$ENV_FILE"; then
  echo
  echo "❌ META_ACCESS_TOKEN not set in $ENV_FILE"
  echo "  Open $ENV_FILE and paste your Meta Marketing API token after 'META_ACCESS_TOKEN='."
  echo "  Get it from the source machine's ~/.openclaw/workspace/.env or from"
  echo "  https://developers.facebook.com/tools/explorer/ (with a long-lived exchange)."
  exit 1
fi
echo "✓ META_ACCESS_TOKEN is set"

# Also make GOOGLE_SERVICE_ACCOUNT_FILE point to the file we just verified,
# so scripts that read it from env (auto_rebuild_dashboard.py local-deploy
# path, validate_reports.py) work without further config.
if ! grep -q '^GOOGLE_SERVICE_ACCOUNT_FILE=' "$ENV_FILE"; then
  echo "GOOGLE_SERVICE_ACCOUNT_FILE=$SA" >> "$ENV_FILE"
  echo "✓ Added GOOGLE_SERVICE_ACCOUNT_FILE to .env"
fi

# ── Step 3: Python venv + deps ─────────────────────────────────────────────
if [[ ! -d "$REPO_ROOT/.venv" ]]; then
  echo "→ Creating .venv..."
  python3 -m venv "$REPO_ROOT/.venv"
fi
# shellcheck disable=SC1091
source "$REPO_ROOT/.venv/bin/activate"
pip install --quiet -r "$REPO_ROOT/requirements.txt"
echo "✓ Python deps installed in .venv"

# ── Step 4: Smoke test — can we open the GHA sheet? ────────────────────────
echo "→ Smoke test: opening the GHA reports sheet..."
set -a; . "$REPO_ROOT/config/sheets.env"; set +a
python3 - <<'PY'
import os, gspread
from google.oauth2.service_account import Credentials
sa = os.environ['GOOGLE_SERVICE_ACCOUNT_FILE']
sid = os.environ['REPORTS_SHEET_ID']
creds = Credentials.from_service_account_file(sa, scopes=['https://www.googleapis.com/auth/spreadsheets'])
sh = gspread.authorize(creds).open_by_key(sid)
print(f"✓ Opened: {sh.title}")
print(f"  {len(sh.worksheets())} tabs visible")
PY

echo
echo "🎉 Bootstrap complete. You can now run any script locally:"
echo "    source .venv/bin/activate"
echo "    set -a; . config/accounts.env; . config/sheets.env; set +a"
echo "    python3 scripts/today_live_report.py"
echo "    python3 scripts/validate_reports.py --date \$(date +%Y-%m-%d)"
echo
echo "Then open out/today_live.html or out/ntn_filtered.html in a browser."
