# EC2 deployment for v2 NTN dashboard

Run the v2 pipeline (ingest → classify → build → Cloudflare deploy) on
the existing EC2 host instead of GitHub Actions. Sidesteps GHA's
per-minute billing wall.

## Why

GHA started rejecting every job in mid-May 2026 with:

> "The job was not started because recent account payments have failed
> or your spending limit needs to be increased."

EC2 is already paid for (the `desistuddmuffyn.in` legacy dashboard runs
there). Adding the v2 pipeline costs zero extra — same instance, same
hour.

## One-time setup (~10 min)

SSH to the EC2 host (`ssh ec2-user@<EC2_HOST>`) and run:

```bash
# 1. Clone the repo
cd ~
git clone https://github.com/pulkit1165/meta-ads-reports.git
cd meta-ads-reports

# 2. Install Python deps (in a venv or system-wide; system-wide assumed here)
sudo yum install -y python3 python3-pip git nodejs   # Amazon Linux 2023
pip3 install --user -r requirements.txt

# 3. Install wrangler for Cloudflare Pages deploys
sudo npm install -g wrangler@3.99.0

# 4. Create the secrets file. Copy these from GitHub Secrets one by one.
nano /home/ec2-user/meta-ads-reports/.env
```

`.env` contents (chmod 600 after creating):

```bash
# Meta
META_ACCESS_TOKEN=<from Meta Business Suite>

# Shopify (3 portals)
SHOPIFY_STORE_URL=studd-muffyn.myshopify.com
SHOPIFY_ACCESS_TOKEN=<...>
SHOPIFY_STORE_URL_SML=<...>
SHOPIFY_ACCESS_TOKEN_SML=<...>
SHOPIFY_STORE_URL_NBP=<...>
SHOPIFY_ACCESS_TOKEN_NBP=<...>

# Google Sheets — paste the service-account JSON to disk and point here
GOOGLE_SERVICE_ACCOUNT_FILE=/home/ec2-user/meta-ads-reports/google-service-account.json

# Cloudflare Pages (for the deploy step)
CLOUDFLARE_ACCOUNT_ID=<from CF dashboard>
CLOUDFLARE_API_TOKEN=<the existing meta-ads-reports-pages-deploy token>

# Optional overrides
# BACKFILL_DAYS=2     # default is today + yesterday
```

Then drop the Google service account JSON in place:

```bash
nano /home/ec2-user/meta-ads-reports/google-service-account.json
# Paste the entire JSON, save, then:
chmod 600 google-service-account.json .env
```

## Schedule (cron)

The hourly cron runs `scripts/v2/ec2_hourly.sh`, which:
1. `git pull` to grab the latest code
2. ingest Meta + Shopify into `state/ntn.db`
3. classify ads
4. build the v2 dashboard HTML
5. deploy to Cloudflare Pages

Install the crontab entry:

```bash
crontab -e
# Add this line:
30 * * * * /home/ec2-user/meta-ads-reports/scripts/v2/ec2_hourly.sh
```

Runs at 30 minutes past every hour, 24/7. A flock-based lock ensures
two crons can't trample each other if one overruns.

Logs append to `/home/ec2-user/v2-ingest.log` — tail it during a run:

```bash
tail -f ~/v2-ingest.log
```

## Smoke test

After setup, run the script manually once to make sure everything's wired:

```bash
/home/ec2-user/meta-ads-reports/scripts/v2/ec2_hourly.sh
```

Successful output ends with a DB-row-count summary and the line
`✓ v2-ingest run complete`. The dashboard at
https://meta-ads-reports.pages.dev/v2/categories should show
`updated_at` matching the run time within ~3 minutes.

## Turning off GHA

Once EC2 cron is verified for ~24 hours, disable the GHA schedule to
stop the billing block from re-triggering:

```yaml
# .github/workflows/v2-ingest.yml — remove the schedule: section or
# leave it as a manual-only workflow_dispatch.
```

The Cloudflare Worker can keep doing `/ping-deploy` if you still want
operator-triggered manual rebuilds.
