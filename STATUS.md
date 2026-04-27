# STATUS

> Living document. The Claude on whichever machine the user is currently working on keeps this current. Read this after `git pull` to see what's actively in flight, what just got shipped, and what the user is thinking about next.

**Last updated:** 2026-04-27 by Claude on machine 1 (`/Users/pulkitsharma/meta-ads-reports`)

---

## What's just shipped (last few sessions)

- Cross-page nav on all 3 dashboards (NTN / Today Live / Categories) — current page highlighted, others linked
- Category Heads summary KPI strip on top of each category panel + sheet tab — 8 spend-weighted cards: Active Ads, Spend, Revenue, ROAS, CTR, CPM, CPV, CPR/1k Reach
- `/categories` Cloudflare Pages URL — single-page tabbed dashboard with all 8 category tabs, deploys hourly via `today-live.yml`
- 7-day per-category history cached to `state/category_history.json` so hourly runs don't re-fetch 50 Meta API calls
- KPI Daily auto-builder (`scripts/kpi_daily_builder.py`) — populates the dashboard's per-portal Meta KPI cards from Meta API, replaces operator's manual fill
- NTN dashboard date columns extended through yesterday using GHA tracker tabs (Adspend + ROAS derived; Orders/Revenue still `—` until Shopify lands)
- Cumulative closures parser fix (Part-1 fallback, "None yet" detection, per-day dedup)
- Cloudflare Pages live at https://meta-ads-reports.pages.dev — deploys via `cloudflare/wrangler-action@v3`, gated on `CLOUDFLARE_PAGES_PROJECT` repo variable
- ARCHITECTURE.md + bootstrap_local.sh + 3-path setup recipe for new machines

## In flight / paused

1. **Shopify integration** — paused. User pasted SM/SML/NBP Shopify access tokens in chat earlier (compromised, told to rotate). Some new tokens added as GitHub secrets (`SHOPIFY_ACCESS_TOKEN`, `SHOPIFY_ACCESS_TOKEN_SML`, `SHOPIFY_STORE_URL`, `SHOPIFY_STORE_URL_NBP`) but `SHOPIFY_ACCESS_TOKEN_NBP` and `SHOPIFY_STORE_URL_SML` are still missing. Once all 6 secrets are in place + tokens are rotated, build `scripts/shopify_new_returning.py` to auto-fill Orders/Revenue/N-R%/C1-C6 from Shopify.

2. **3 empty category tabs** — 24K Jewellery, Perfumes, Aibot all have 0 ads matching the keyword rules in `derive_category_v2()`. User needs to share 2-3 sample campaign names per category so we can extend the keyword list. Currently empty tabs render placeholder text.

3. **NTN sheet operator backlog** — operator stopped updating the NTN source sheet past 24-Apr (Orders/Revenue/N-R%/Sales Block/C1-C6 cohorts). Dashboard fills Spend+ROAS for newer dates from GHA-derived data. Either resume Shopify integration or operator catches up.

4. **C1-C6 default targets** in the GHA sheet's `C1-C6 Targets` tab are placeholders (30/50/70/75/80/90) — user hasn't tuned them yet.

5. **External cron pinger** for GHA reliability — discussed but not built. GHA's scheduled runs miss slots (today-live + closing-watchlist skipped 04:00 UTC slot 27-Apr). Could set up cron-job.org to hit `workflow_dispatch` API for guaranteed firing.

## Other proposals offered but not built

User had earlier asked "what other reports could help" — I proposed 7 ideas, none built yet:
- Spend Pacing vs ₹2L plan
- Day-0 Performance Watch
- Top 10 / Bottom 10 by today's ROAS
- Audience Mix Drift (today vs 7-day avg)
- Spend Velocity Warnings
- Creative Fatigue Watch
- Weekly Recap (Sunday rollup)

## Multi-machine setup

User has 3 machines now:
- Machine 1: `/Users/pulkitsharma/meta-ads-reports/` (this one — pulkit1165 GitHub user)
- Machine 2: `/Users/apple/Desktop/claude/meta-ads-reports/`
- Machine 3: being set up via `bootstrap_local.sh`

Cred transfer between machines: AirDrop the JSON + paste the token. Each machine has its own local copies of `.env` and `google-service-account.json` (gitignored). User declined `age`-encrypted-in-git approach for now.

## Read this before doing anything

- [ARCHITECTURE.md](ARCHITECTURE.md) — system map, workflows, sheet IDs, troubleshooting
- [docs/META_REPORTING_MASTER_DOC.md](docs/META_REPORTING_MASTER_DOC.md) — business rules, audience taxonomy, ROAS thresholds
- [docs/CLOUDFLARE_PAGES_SETUP.md](docs/CLOUDFLARE_PAGES_SETUP.md) — how the live URL got set up
- [SECRETS.md](SECRETS.md) — required GitHub secrets (mostly set; Shopify ones partial)

## Etiquette for the agent on duty

- Update this `STATUS.md` after each meaningful change (commit it).
- Don't take destructive actions without explicit user confirmation.
- Auto mode is on per the user — execute, don't over-plan.
- Never act on credentials pasted into chat; recommend rotation.
