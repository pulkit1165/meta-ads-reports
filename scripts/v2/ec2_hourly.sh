#!/bin/bash
# EC2 hourly cron for the v2 NTN dashboard pipeline.
#
# Replaces the GitHub Actions v2-ingest + today-live workflows so the
# dashboard stops depending on GHA's per-minute billing. Runs end-to-end:
#   - git pull (gets latest code)
#   - load secrets from .env
#   - ingest Meta + Shopify into state/ntn.db
#   - classify ads
#   - build the v2 dashboard HTML
#   - deploy to Cloudflare Pages
#
# Install: see docs/EC2_DEPLOYMENT.md. Run from /home/ec2-user/meta-ads-reports.
# Recommended crontab entry:  30 * * * * /home/ec2-user/meta-ads-reports/scripts/v2/ec2_hourly.sh

set -uo pipefail   # NOT -e — one stage failing shouldn't kill subsequent ones

REPO="/home/ec2-user/meta-ads-reports"
LOG="/home/ec2-user/v2-ingest.log"
LOCK="/tmp/v2-ingest.lock"

# Single-instance lock — concurrent crons would step on each other's DB.
exec 200>"$LOCK"
if ! flock -n 200; then
  echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] another v2-ingest already running, skipping"
  exit 0
fi

# Tee output to the log file too — useful for debugging from `tail -f`.
exec > >(tee -a "$LOG") 2>&1

echo
echo "════════════════════════════════════════════════════════════════════"
echo "v2-ingest run @ $(date -u '+%Y-%m-%dT%H:%M:%SZ') ($(TZ=Asia/Kolkata date '+%H:%M IST'))"
echo "════════════════════════════════════════════════════════════════════"

cd "$REPO" || { echo "FATAL: repo dir $REPO not found"; exit 1; }

# 1. Pull latest code (no merge — we only run from main).
echo "→ git fetch + reset --hard origin/main"
git fetch --quiet origin main
git reset --hard origin/main --quiet
echo "  at $(git rev-parse --short HEAD): $(git log -1 --format='%s')"

# 2. Load env from .env (Meta token, Shopify tokens, CF token, etc.)
if [ ! -f .env ]; then
  echo "FATAL: $REPO/.env not found — see docs/EC2_DEPLOYMENT.md"
  exit 1
fi
set -a
. ./.env
set +a

# 3. Load ad-account IDs from the committed config (same as GHA does).
set -a
. <(grep -E '^[A-Z][A-Z0-9_]*=' config/accounts.env)
set +a

# 4. Schema migrations + lookup seeds (both idempotent).
python3 scripts/v2/db_init.py
python3 scripts/v2/seed_lookups.py

# 5. Sync the SKU sheet — non-fatal if Sheets API hiccups.
python3 scripts/v2/sync_ntn_from_sheet.py || echo "   ⚠️  sync_ntn failed (non-fatal)"

# 6. Meta + Shopify ingest (default --days 2 = today + yesterday).
DAYS="${BACKFILL_DAYS:-2}"
python3 scripts/v2/ingest_meta.py    --days "$DAYS"
python3 scripts/v2/ingest_shopify.py --days "$DAYS"

# 7. Classify ads (incremental — only new/v<current).
python3 scripts/v2/classify_ads.py

# 8. Build v2 dashboard.
mkdir -p out/v2
python3 scripts/v2/build_dashboard.py --days 30 --out out/v2/categories.html

# 9. Deploy to Cloudflare Pages.
#    Needs wrangler installed (npm i -g wrangler@3.99.0) and
#    CLOUDFLARE_API_TOKEN + CLOUDFLARE_ACCOUNT_ID in .env.
if [ -f out/v2/categories.html ] && [ -n "${CLOUDFLARE_API_TOKEN:-}" ]; then
  # Index page serves the v2 dashboard at /
  cp out/v2/categories.html out/index.html
  # Cache headers — short max-age so the dashboard reflects new builds quickly.
  cat > out/_headers <<'HDR'
/*
  Cache-Control: public, max-age=60, must-revalidate
  Access-Control-Allow-Origin: *
HDR
  CLOUDFLARE_ACCOUNT_ID="$CLOUDFLARE_ACCOUNT_ID" \
  CLOUDFLARE_API_TOKEN="$CLOUDFLARE_API_TOKEN" \
  wrangler pages deploy out \
      --project-name=meta-ads-reports \
      --branch=main \
      --commit-dirty=true \
      --commit-message="ec2-cron @ $(date -u '+%H:%MZ')"
else
  echo "   ⚠️  skipping Cloudflare deploy (missing token or build)"
fi

# 10. DB summary so the log shows whether numbers look healthy.
python3 - <<'PY'
import sqlite3
conn = sqlite3.connect('state/ntn.db')
for tbl in ('meta_ads_daily', 'meta_ads_meta', 'meta_campaigns',
            'meta_adsets', 'shopify_orders', 'ingest_log'):
    try:
        n = conn.execute(f'SELECT COUNT(*) FROM {tbl}').fetchone()[0]
        print(f'  {tbl:25s} {n:>10,}')
    except sqlite3.OperationalError:
        print(f'  {tbl:25s} (not created yet)')
PY

echo "✓ v2-ingest run complete @ $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
