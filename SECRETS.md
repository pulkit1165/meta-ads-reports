# Required GitHub secrets

The workflows won't work until you add these two secrets in the GitHub repo.

## Where to add them

Open: https://github.com/pulkit1165/meta-ads-reports/settings/secrets/actions → click **New repository secret**.

## 1. `META_ACCESS_TOKEN`

Your Meta Marketing API access token.

- **Scope required:** `ads_read` (minimum) + `ads_management` if any script mutates
- **Type:** preferably a **long-lived System User token**. Short user tokens expire in ~1 hour and will break scheduled jobs.
- **Value:** paste the raw token string, no quotes

## 2. `GOOGLE_SERVICE_ACCOUNT_JSON`

Full contents of your Google service-account JSON key.

Run this on your Mac to copy the JSON contents to the clipboard:

```bash
pbcopy < /Users/pulkitsharma/.openclaw/workspace/google-service-account.json
```

Then paste into the secret value field. It will be multiline — that's fine, GitHub handles it.

The workflows write this to `google-service-account.json` in the runner workspace before scripts run, and `GOOGLE_SERVICE_ACCOUNT_FILE` env var is set to that path.

> Make sure the service account email has **Editor** access to both Google Sheets:
> - Campaign Tracker: `11IAPsJlil75aehYf5IzpSaTCLcAgPk9-57p6ZuPNNQM`
> - NTN Dashboard: `1squ0JkqwiyFwIMRmqWc3q_AWHQtihn5o4dbDGyv7sAY`
>
> You can get the email with: `jq -r .client_email < /Users/pulkitsharma/.openclaw/workspace/google-service-account.json`

## What's NOT a secret

The 15 ad account IDs are **not secrets** — they're just identifiers. They live in [`config/accounts.env`](config/accounts.env), committed to the repo, and each workflow loads them via `cat config/accounts.env >> "$GITHUB_ENV"`.

## What's skipped for now (per your Phase 3 decisions)

- **WhatsApp API credentials** — you asked to wire WA "later". The 1 PM closing alert runs without `--notify`, so the report lands in the sheet but no WA message is sent.
- **EC2 SSH key** — the original EC2 deploy for Reports 6 & 7 keeps running. Our copies here run the build steps (HTML lands in `out/` as an artifact), but don't deploy. Set `ENABLE_EC2_DEPLOY=1` + add `EC2_SSH_KEY` / `EC2_HOST` secrets later if you want to switch the deploy over.
- **Shopify tokens** — none of the canonical scripts actually import Shopify, so nothing to add.

## Verifying everything works

After adding both secrets, trigger a manual run:

1. Go to **Actions** → **Daily pipeline (4:30 AM IST)** → **Run workflow**
2. Watch the logs. If it fails, the error message will tell you what's missing.
3. On success, check the Campaign Tracker sheet for the expected new tabs.

## Schedule summary (UTC ← IST)

| Workflow | Cron (UTC) | IST time |
|----------|------------|----------|
| `daily.yml` | `0 23 * * *` | 04:30 daily |
| `closing-watchlist.yml` | `30 4,6,9,12 * * *` | 10:00 / 12:00 / 15:00 / 18:00 |
| `live-monitor.yml` | `0 1,4,7,10,13,22 * * *` | 06:30 / 09:30 / 12:30 / 15:30 / 18:30 / 03:30 |
| `live-closing-alert.yml` | `30 7 * * *` | 13:00 daily |
