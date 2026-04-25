# meta-ads-reports — Architecture

Single-source-of-truth for anyone (human or agent) picking up work on this repo from a fresh machine. Read this before touching anything. Last updated: 2026-04-25.

---

## What this repo does (1-paragraph elevator pitch)

Automated Meta Ads reporting for a multi-store Shopify e-commerce business (Studd Muffyn portals: SM, SML, NBP — 15 ad accounts). The system pulls live + historical performance from the Meta Marketing API, writes structured tabs into a Google Sheet, and renders self-contained HTML dashboards. Everything runs on GitHub Actions on a free-tier private repo. The original cron-based version still runs from `~/.openclaw/workspace/` (writing to a separate "live" sheet) — this repo is the migrated, audited version that writes to its own sheet so the two don't conflict.

---

## Repository layout

```
meta-ads-reports/
├── ARCHITECTURE.md              # ← you are here
├── README.md                    # short status / phase overview
├── SECRETS.md                   # how to add the 2 required GitHub secrets
├── requirements.txt             # python-dotenv, requests, gspread, google-auth
├── .env.example                 # local-dev template
├── .gitignore                   # ignores .env, service-account.json, state/*, out/*
├── config/
│   ├── accounts.env             # 15 ad-account IDs (not secret — committed)
│   └── sheets.env               # GHA-owned sheet ID (REPORTS_SHEET_ID)
├── scripts/                     # all Python — 13 files
├── docs/
│   └── META_REPORTING_MASTER_DOC.md   # business-side doc (audiences, ROAS rules, contacts)
├── .github/
│   ├── actions/state-sync/      # composite action — manages 'state' branch
│   └── workflows/               # 5 cron workflows
├── state/                       # snapshot files (gitignored on main; committed to 'state' branch)
└── out/                         # generated artifacts (HTML dashboards, gitignored)
```

---

## Data flow

```
                 ┌─────────────────────────┐
   Meta Ads API ─┤  scripts/today_*.py     ├─► Google Sheet "Meta Ads Reports (GHA)"
                 │  scripts/closing_*.py   │   (1hJ3IS2VDtTAEy...)
                 │  scripts/live_*.py      │
                 │  scripts/campaign_*.py  │
                 │  scripts/creative_*.py  │
                 └────────────┬────────────┘
                              │
                              ▼
                 ┌─────────────────────────┐
                 │  out/today_live.html    │  ← refreshed hourly
                 │  out/ntn_filtered.html  │  ← embeds today-live + reads NTN sheet
                 │  out/creative_*.html    │
                 └─────────────────────────┘
                              │
                 GitHub Actions artifacts
                 (downloadable from each run)
```

NTN-side data flow is separate and read-only:
```
   NTN sheet (1squ0Jkq...)  ──►  scripts/auto_rebuild_dashboard.py  ──►  out/ntn_filtered.html
   (manually-populated;
    EC2 cron writes here)
```

State-branch flow (snapshot persistence between workflow runs — replaces `/tmp/`):
```
   workflow run #1  ──►  state/closing_snapshot.json  ──►  push to 'state' branch
                                                             │
   workflow run #2  ◄── pull from 'state' branch ◄──────────┘
```

---

## Google Sheets

| Sheet | ID | Role | Who writes |
|---|---|---|---|
| **Campaign Tracker (original)** | `11IAPsJlil75aehYf5IzpSaTCLcAgPk9-57p6ZuPNNQM` | Live production sheet — daily tracker, closing, live monitor, etc. | EC2/local cron (from `~/.openclaw/workspace/`). **This repo never writes to it.** |
| **Meta Ads Reports (GHA)** | `1hJ3IS2VDtTAEyyJIV__jvts9CMQdYhyxKAfWKtrkUH4` | Where THIS repo writes everything: tracker, closing, live monitor, today's live, C1-C6 targets, cumulative closures | This repo's workflows |
| **NTN Dashboard source** | `1squ0JkqwiyFwIMRmqWc3q_AWHQtihn5o4dbDGyv7sAY` | Manually-populated category dashboard data (orders, revenue, audience splits, C1-C6 cohorts) | Operator / business team. This repo only **reads** from it. |

`scripts` resolve which sheet to use via `REPORTS_SHEET_ID` env var (defaults to the GHA-owned sheet ID hard-coded as fallback). The original sheet ID is never the default in this repo — accidental writes to it are impossible.

**Service account that writes:** `antriksh-bot@antriksh-meta-reports.iam.gserviceaccount.com` — must have Editor access on the GHA sheet and read on the NTN sheet.

---

## Workflows (`.github/workflows/`)

All times are IST (workflow env sets `TZ=Asia/Kolkata`). Cron is in UTC.

| Workflow | IST schedule | UTC cron | What it runs |
|---|---|---|---|
| `daily.yml` | 04:30 daily | `0 23 * * *` | `run_daily_reports.py` (tracker → budget → creative → closing camps) + `build_creative_dashboard.py` + `auto_rebuild_dashboard.py` + `cumulative_closures.py`. Full historical refresh. |
| `closing-watchlist.yml` | 10:00 / 12:00 / 15:00 / 18:00 | `30 4,6,9,12 * * *` | `closing_watchlist.py` — appends to `🔴 Closing DD MMM YY` tab; uses state branch for "closed since last run" detection. |
| `live-monitor.yml` | 06:30 / 09:30 / 12:30 / 15:30 / 18:30 / 03:30 | `0 1,4,7,10,13,22 * * *` | `live_camp_monitor.py` — overwrites `🔴 Live Monitor` tab with current snapshot. |
| `live-closing-alert.yml` | 13:00 daily | `30 7 * * *` | `live_camp_monitor.py --closing` (no `--notify` since WA isn't wired). |
| `today-live.yml` | hourly 09:30–19:30 | `0 4-14 * * *` | `today_live_report.py` (sheet + standalone HTML) + `auto_rebuild_dashboard.py` (rebuilds NTN dashboard with today's data embedded). |

**Concurrency**: each workflow has `concurrency.group: <name>`, `cancel-in-progress: false` — overlapping fires queue.

**Permissions**: `contents: write` (needed by state-sync to push to the `state` branch).

**Schedule reliability caveat**: GitHub Actions cron is best-effort — runs can be delayed up to ~60 min during high-load periods. We've seen this affect `closing-watchlist` (occasionally skipped). For hard refresh, manually trigger via Actions tab → "Run workflow".

---

## Composite action: `state-sync`

Path: `.github/actions/state-sync/action.yml`. Used by every workflow that needs snapshot persistence between runs.

```yaml
- uses: ./.github/actions/state-sync
  with: { direction: restore }
# ... script runs, writes to state/foo.json ...
- uses: ./.github/actions/state-sync
  if: always()
  with: { direction: save }
```

How it works:
1. `restore`: fetches the orphan `state` branch and rsyncs its contents into `state/`. If the branch doesn't exist yet (first run), it's a no-op.
2. `save`: rsyncs `state/` back into a worktree on the `state` branch, commits, pushes. Includes retry-on-race (5 attempts with rebase) for concurrent workflow contention.

The `state` branch is **orphan** — it has no shared history with `main`. It only contains files like `closing_snapshot_2026-04-25.json`, `live_monitor_snapshot.json`, `tracker_cache_act_*.json`. This is the GHA replacement for `/tmp/` (which is wiped between runner instances).

Author identity for state commits is set via `GIT_AUTHOR_*` / `GIT_COMMITTER_*` env vars (NOT git config) — needed because git rebase reads them too.

---

## Scripts (`scripts/`)

### Data-fetching + sheet-writing (Meta API → Google Sheet)

| Script | Output tab(s) | Cadence | Notes |
|---|---|---|---|
| `campaign_tracker_builder.py` | `SM/SML/NBP DD MMM YY` | Daily 04:30 | Pulls `--all` accounts in one pass to avoid the per-account-overwrite bug. ROAS in 4 attribution windows (1d/2d/3d/7d). |
| `campaign_tracker_reports.py` | `📊 Reports DD MMM YY` | Daily 04:30 | Reads tracker tabs + `state/tracker_cache_*.json` for accurate per-window ROAS. Sections: Category, Audience, Product, Unmapped. |
| `closing_camps_report.py` | Appends to `📊 Reports DD MMM YY` | Daily 04:30 + 13:00 | Live Meta API. Filters: ROAS < 1.0, ₹2L close-budget cap with age-bucket allocation (40/30/30 %). |
| `closing_watchlist.py` | `🔴 Closing DD MMM YY` | 10/12/15/18 IST | SM only. New `💀 DECISION TRIGGERED` section at top (≥30% spent + ROAS≤1). State branch tracks morning + per-run snapshots for "closed since" detection. |
| `live_camp_monitor.py` | `🔴 Live Monitor` (overwritten) | Every 3 hrs | All portals. Computes Δ ROAS vs previous run. Optional `--closing` mode appends close recommendations. |
| `creative_report.py` | `📊 Creative Report DD MMM YY` | Daily 04:30 | Ad-level. Creative type classification (Paras/Partnership/Motion/Static/Catalogue/Testing/Others) by name keyword. |
| `today_live_report.py` | `🔴 Today's Live DD MMM YY` (overwritten each run) | Hourly during business hours | SM only. Sections: live tracker by Type×Audience, daily success rate buckets, best creatives, creative type mix. **Also writes `out/today_live.html` and powers the embedded section in NTN dashboard.** |
| `cumulative_closures.py` | `📈 Cumulative Closures` | Daily 04:30 | Reads every `🔴 Closing` tab in the sheet, aggregates totals + by-type breakdown across all days. |

### HTML dashboard generators

| Script | Output | Cadence | Notes |
|---|---|---|---|
| `auto_rebuild_dashboard.py` | `out/ntn_filtered.html` | Daily 04:30 + hourly | Reads NTN source sheet via section-header lookup (resilient to row drift). Embeds today-live data at top. C1-C6 cohort actuals vs editable targets (target tab auto-bootstrapped on first run). N/R analytics with 7-day avg + delta. **Does not deploy** to EC2 unless `ENABLE_EC2_DEPLOY=1`. |
| `build_creative_dashboard.py` | `out/creative_dashboard_<date>.html` | Daily 04:30 | Self-contained creative perf HTML by category × creative type. **Does not deploy** to EC2 from this repo. |

### Helpers + orchestrators

| Script | Role |
|---|---|
| `product_catalogue.py` | Pure module. Maps campaign-name keywords → (product, category, segment, type). Used by tracker builder and reports. |
| `run_daily_reports.py` | The 04:30 IST orchestrator. Calls tracker_builder `--all`, then reports, then creative_report, then closing_camps_report. Subprocess-based; passes `META_ACCESS_TOKEN` through env. |
| `validate_reports.py` | Diagnostic. Compares sheet contents against Meta API ground truth for a given date. Run manually: `python3 scripts/validate_reports.py --date 2026-04-24`. Exit 0 = all checks green. |

---

## Configuration

### Environment variables

Three layers, **process env > .env file** wins where both are present:

| Var | Purpose | Where set |
|---|---|---|
| `META_ACCESS_TOKEN` | Meta Marketing API token | GH secret (workflow) / `.env` (local) |
| `GOOGLE_SERVICE_ACCOUNT_FILE` | Path to Google service-account JSON | Workflow writes from `GOOGLE_SERVICE_ACCOUNT_JSON` secret; local: just point to the file |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Full JSON contents (workflow only) | GH secret |
| `REPORTS_SHEET_ID` | Which Google Sheet to write reports to | `config/sheets.env` (loaded into `$GITHUB_ENV` per workflow) |
| `META_REPORTS_STATE_DIR` / `META_REPORTS_OUT_DIR` | Override default `state/` and `out/` paths | Optional |
| `ENABLE_EC2_DEPLOY` + `EC2_SSH_KEY` + `EC2_HOST` | Opt-in EC2 deploy of `ntn_filtered.html` (currently unused — original EC2 cron is canonical for live dashboard) | Optional |
| 15× `SM_*` / `SML_*` / `NBP_*` | Ad account IDs (not secret) | `config/accounts.env` (loaded into `$GITHUB_ENV` per workflow) |

### What's NOT a secret (kept in committed config files)

- All 15 ad-account IDs — `config/accounts.env`
- The GHA sheet ID — `config/sheets.env`

These get loaded via `grep -E '^[A-Z][A-Z0-9_]*=' config/<file> >> "$GITHUB_ENV"` in each workflow (the `grep` filter is necessary — `$GITHUB_ENV` rejects comments and blank lines).

### Required GitHub secrets

Two only — see `SECRETS.md` for the full setup walkthrough:

1. `META_ACCESS_TOKEN`
2. `GOOGLE_SERVICE_ACCOUNT_JSON`

Add at: https://github.com/pulkit1165/meta-ads-reports/settings/secrets/actions

---

## Bootstrapping a new machine

### Fast path (recommended) — `scripts/bootstrap_local.sh`

```bash
git clone git@github.com:pulkit1165/meta-ads-reports.git
cd meta-ads-reports

# Place the two creds manually:
#   1. Copy google-service-account.json from your source machine
#      (~/.openclaw/workspace/google-service-account.json) to ./google-service-account.json
#      AirDrop / 1Password secure note / encrypted USB. NEVER email or commit.
#   2. Edit .env (auto-created from .env.example on first script run) and paste META_ACCESS_TOKEN.

bash scripts/bootstrap_local.sh
```

The script:
- Verifies the service-account JSON is present + valid + chmod 600
- Creates `.env` from `.env.example` and checks that `META_ACCESS_TOKEN` is set
- Adds `GOOGLE_SERVICE_ACCOUNT_FILE` to `.env` pointing at the file
- Creates `.venv` and pip-installs `requirements.txt`
- Smoke-tests by opening the GHA reports sheet via the service account

If anything's missing it prints exactly what to do and exits non-zero.

### Manual path

```bash
git clone git@github.com:pulkit1165/meta-ads-reports.git
cd meta-ads-reports
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Fill META_ACCESS_TOKEN in .env

# Service account file (from the source machine):
cp ~/.openclaw/workspace/google-service-account.json .
chmod 600 google-service-account.json

# Verify env loads
set -a; . config/accounts.env; . config/sheets.env; set +a
python3 -c "import os; assert os.environ.get('REPORTS_SHEET_ID'); print('env OK')"

# Smoke test
python3 scripts/today_live_report.py
open out/today_live.html
```

### Code-only path (no creds needed)

If you only want to edit code on a 2nd/3rd machine and let GHA run things, skip the credential copy. Just `git pull`, edit, `git push`, and watch the Actions tab.

---

## Open threads / paused work (as of 2026-04-25)

1. **Shopify new-vs-returning customer integration** — *paused after token leak.* User pasted SM/SML/NBP Shopify access tokens directly into chat; tokens are compromised, told user to rotate. When resumed: build `scripts/shopify_new_returning.py` (read Customers + Orders APIs per store) and replace the manual N/R Master values feeding the NTN dashboard's New/Returning section. Secret names earmarked: `SHOPIFY_ACCESS_TOKEN(_SML/_NBP)` + `SHOPIFY_STORE_URL(_SML/_NBP)`.

2. **Live hosting for `ntn_filtered.html` and `today_live.html`** — user picked GitHub Pages but free-tier Pages doesn't support private repos. A public mirror repo would expose business data to GitHub search. Pending decision among:
   - Cloudflare Pages (free, deploys from private repo, optional email-gated access via Cloudflare Access) ← *recommended*
   - Netlify / Vercel (similar)
   - GitHub Pro upgrade ($4/mo) — Pages on private repo, site still public
   - Public mirror repo (hook blocked this once already; would need explicit user re-confirmation)

3. **Auto-discovery of new ad accounts in Meta Business Manager** — token has `ads_read` + `ads_management` only. To enable `/me/adaccounts` discovery, re-issue the token with `business_management` scope added. Until then, new accounts must be added manually to `config/accounts.env` + the `ACCOUNT_MAP` blocks in 5 scripts.

4. **C1-C6 targets** — auto-bootstrapped tab `C1-C6 Targets` in the GHA sheet with default values (30/50/70/75/80/90 per cohort). User edits there, dashboard picks up changes on next build. Defaults are placeholders — user hasn't tuned them yet.

5. **Extra report proposals** offered but not built (greenlight → I implement):
   - Spend Pacing vs ₹2L plan
   - Day-0 Performance Watch
   - Top 10 / Bottom 10 by today's ROAS
   - Audience Mix Drift (today vs 7-day avg)
   - Spend Velocity Warnings (burning too fast for time-of-day)
   - Creative Fatigue Watch (declining ROAS over 1d/3d/7d)
   - Weekly Recap (Sunday rollup)

---

## Troubleshooting playbook

| Symptom | Likely cause | Fix |
|---|---|---|
| Workflow fails at "Load ad account IDs" with `Invalid format` | `cat config/foo.env >> "$GITHUB_ENV"` includes comment lines | Already fixed: workflow uses `grep -E '^[A-Z][A-Z0-9_]*=' config/<file>`. If a new config file is added, use the same grep. |
| `state` branch push fails with `committer identity unknown` | `git rebase` (during retry path) doesn't see the `-c user.name/email` flags from the initial commit | Already fixed: state-sync action exports `GIT_AUTHOR_NAME/EMAIL` + `GIT_COMMITTER_NAME/EMAIL` env vars at the top of the save flow. |
| SM tracker tab has only 1 campaign on a slow day | Genuinely a slow day — verify with `validate_reports.py --date <date>` | If validator says counts match Meta API, it's real. If they diverge, file a bug. |
| `auto_rebuild_dashboard.py` reads wrong rows for a section | Source sheet layout drifted; hardcoded indices stale | Already fixed: extraction uses `find_section('<header text>')` lookups, not row numbers. |
| Daily run wipes per-account data, leaving only the last account's rows on the SM tab | `run_daily_reports.py` looped per-account, builder clears tab each call | Already fixed: uses `--all` (single invocation, dedup by `campaign_id` across accounts). |
| Schedule fired late or skipped | GHA cron is best-effort | Manually dispatch via Actions tab; if persistent, consider an external pinger like cron-job.org calling the `workflow_dispatch` API. |
| Re-run of a failed run reuses old buggy code | GHA "Re-run failed jobs" replays the workflow YAML as it was at original trigger time | Don't use Re-run. Use **Run workflow** for a fresh-code dispatch. |
| Today-Live "# Active" vs "# Had Spend" disagree | These are two different universes (`effective_status=ACTIVE` vs `spend > 0 today`); both can be valid | Working as intended. The mix table shows both columns. |

---

## Memory pointers (Claude / agents)

There are 2 saved memories that complement this doc — see `~/.claude/projects/-Users-pulkitsharma/memory/`:

- `project_meta_ads_reports.md` — open threads, sheet IDs, paused work
- `feedback_secrets_in_chat.md` — never act on credentials leaked into chat; always rotate first

When picking up work in a new session, check those, then re-read this `ARCHITECTURE.md` for the technical layer, then read `docs/META_REPORTING_MASTER_DOC.md` for the business layer.

---

## When making changes

- **Run `validate_reports.py --date <date>`** after touching any data-extraction or sheet-write script.
- **Compile-check** every script edit: `python3 -c "import py_compile; py_compile.compile('scripts/X.py', doraise=True)"`.
- **Validate workflow YAML** before pushing: `python3 -c "import yaml; yaml.safe_load(open('.github/workflows/X.yml'))"`.
- **Don't commit** `.env`, `google-service-account.json`, or any file that would leak a token.
- **Don't push to the `state` branch** manually. The state-sync action owns it.
- **Trigger workflows fresh** via Actions → Run workflow, not by re-running an old failed run.
