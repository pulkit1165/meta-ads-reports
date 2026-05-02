# Cron pinger Worker

A tiny Cloudflare Worker that pings the GitHub `today-live.yml` workflow
every hour at exactly **IST 09:50, 10:50, …, 19:50**. It exists because
GitHub Actions' built-in cron is best-effort and silently drops 50–100% of
scheduled firings on free-tier private repos during peak load — Cloudflare
crons fire reliably (~99.9%) and don't share GHA's queue.

## How it works

1. Cloudflare's scheduler triggers `scheduled()` in [worker.js](worker.js)
   every hour at UTC :20 (= IST :50).
2. The Worker POSTs to GitHub's
   `actions/workflows/today-live.yml/dispatches` endpoint.
3. The today-live workflow runs (~5 min) and Cloudflare Pages auto-deploys
   the refreshed dashboard.

## Deploy

The Worker auto-deploys via [`.github/workflows/deploy-cron-worker.yml`]
whenever `cron-worker/**` files change on `main`. Two GH secrets must exist:

| Secret | Used for | Created when |
|---|---|---|
| `CLOUDFLARE_API_TOKEN` | Wrangler auth to deploy the Worker | Already set ✓ |
| `CLOUDFLARE_ACCOUNT_ID` | CF account scoping | Already set ✓ |
| `WORKFLOW_DISPATCH_PAT` | Worker uses this to call GitHub API | **Must be created (see below)** |

## One-time PAT setup

The Worker needs a GitHub fine-grained PAT to dispatch the workflow.
Create it once:

1. Open https://github.com/settings/personal-access-tokens/new
2. Settings:
   - Token name: `dashboard-pinger`
   - Expiration: **1 year**
   - Repository access: **Only select repositories** → `pulkit1165/meta-ads-reports`
   - Permissions → **Repository permissions → Actions → Read and write**
   - All other permissions: leave as default ("No access")
3. Generate token, copy the value (starts with `github_pat_…`)
4. Add it as a GitHub repo secret:
   ```
   gh secret set WORKFLOW_DISPATCH_PAT --repo pulkit1165/meta-ads-reports
   # paste the token, press enter, ctrl+D
   ```
5. Trigger the deploy workflow once:
   ```
   gh workflow run deploy-cron-worker.yml --repo pulkit1165/meta-ads-reports
   ```

After that, the Worker is live and the dashboard refreshes every hour
at :50 IST forever (until the PAT expires in 1 year).

## Verify it's working

After first deploy, the Worker URL is `https://meta-ads-cron-pinger.<your-cf-subdomain>.workers.dev`.

- Visit `/` for a status line.
- Visit `/ping` to manually fire a dispatch (returns the GH API response).
- Check Cloudflare dashboard → Workers → meta-ads-cron-pinger → Cron events
  to see the scheduled firing history.

## Disable / remove

If you ever want to go back to GHA-only cron:

```
cd cron-worker
npx wrangler delete --name meta-ads-cron-pinger
```

…or delete the Worker manually from the Cloudflare dashboard.
